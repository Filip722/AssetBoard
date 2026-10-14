"""Categories page in Chromium (F2.1): HTMX inline edit, reorder, confirm, and no CSP violations."""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from assetboard.app import create_app
from assetboard.config import Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium tests",
)

# Records every CSP violation the page reports (inline styles, inline scripts, eval).
VIOLATION_RECORDER = """
window.__cspViolations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__cspViolations.push(event.violatedDirective + " " + (event.blockedURI || "inline"));
});
"""


def test_f21_categories_in_a_browser(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="browser-test-key",
        )
    )
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    server_thread.start()
    deadline = time.monotonic() + 10
    while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
        threading.Event().wait(0.01)

    try:
        assert server.started
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(color_scheme="light")
            context.add_init_script(VIOLATION_RECORDER)
            console_errors: list[str] = []
            page = context.new_page()
            page.on(
                "console",
                lambda message: (
                    console_errors.append(message.text) if message.type == "error" else None
                ),
            )
            base = f"http://127.0.0.1:{port}"
            page.goto(f"{base}/", wait_until="networkidle")
            page.get_by_label("Setup token").fill(app.state.setup_token)
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Display name").fill("Browser Admin")
            page.get_by_label("Password").fill("Correct Horse Battery!")
            page.get_by_label("Workspace name").fill("Browser Home")
            page.get_by_role("button", name="Create administrator").click()
            page.wait_for_url(f"{base}/")

            page.locator("[data-categories-link]").click()
            page.wait_for_url(f"{base}/categories")
            page.get_by_role("button", name="Add suggested categories").click()
            expense = page.locator('[data-section="expense"] [data-category-name]')
            expect(expense.first).to_have_text("Food")
            assert page.url == f"{base}/categories"  # an HTMX swap, not a navigation

            # Inline edit: the row turns into a form, saving swaps the list back in place.
            food = page.locator("li[data-category]", has_text="Food").first
            food.get_by_role("link", name="Edit").click()
            editing = page.locator("li[data-editing]")
            expect(editing).to_be_visible()
            editing.get_by_label("Name").fill("Groceries")
            editing.get_by_role("button", name="Save").click()
            expect(expense.first).to_have_text("Groceries")
            expect(page.locator("[data-notice]")).to_have_text("Category saved.")

            # Reorder with the buttons.
            page.get_by_role("button", name="Move Eating out up").click()
            expect(expense.first).to_have_text("Eating out")

            # Delete asks for confirmation through the CSP-safe dialog.
            gifts = page.locator("li[data-category]", has_text="Gifts")
            gifts.get_by_role("button", name="Delete").click()
            expect(page.locator("#confirm-dialog")).to_contain_text("Delete the category Gifts?")
            page.get_by_role("button", name="Continue").click()
            expect(page.locator("[data-notice]")).to_have_text("Category deleted.")
            expect(page.locator("li[data-category]", has_text="Gifts")).to_have_count(0)

            assert page.evaluate("window.__cspViolations") == []
            assert not [e for e in console_errors if "Content Security Policy" in e]
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
