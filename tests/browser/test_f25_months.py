"""Month page in Chromium (F2.5): navigation, the native month picker, close/reopen with the
confirm dialog, and no CSP violations."""

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
from assetboard.domain.months import YearMonth

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


def test_f25_month_page_in_a_browser(tmp_path: Path) -> None:
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

            # The nav goes to today's month (the real clock: compare with the server's).
            today = YearMonth.from_date(app.state.clock.today("UTC"))
            page.locator("[data-month-link]").click()
            page.wait_for_url(f"{base}/m/{today}")
            heading = page.locator("h1")
            expect(heading).to_contain_text(str(today.first_day.year))

            # Previous, next and "Today".
            previous = today.prev()
            assert previous is not None
            page.locator("[data-month-prev]").click()
            page.wait_for_url(f"{base}/m/{previous}")
            page.locator("[data-month-next]").click()
            page.wait_for_url(f"{base}/m/{today}")
            page.goto(f"{base}/m/2001-05")
            page.locator("[data-month-today]").click()
            page.wait_for_url(f"{base}/m/{today}")

            # The native month picker (Chromium) submits a plain GET form.
            page.locator("#month-picker").fill("2025-03")
            page.get_by_role("button", name="Go", exact=True).click()
            page.wait_for_url(f"{base}/m/2025-03")
            expect(heading).to_contain_text("March 2025")

            # Closing asks for confirmation through the CSP-safe dialog; cancel keeps it open.
            page.locator("[data-month-close]").click()
            dialog = page.locator("#confirm-dialog")
            expect(dialog).to_contain_text("Close March 2025?")
            page.get_by_role("button", name="Cancel").click()
            expect(page.locator("[data-closed-banner]")).to_have_count(0)
            page.locator("[data-month-close]").click()
            page.get_by_role("button", name="Continue").click()
            page.wait_for_url(f"{base}/m/2025-03?notice=closed")
            expect(page.locator("[data-closed-banner]")).to_contain_text("This month is closed.")
            expect(page.locator("[data-closed-banner]")).to_contain_text("by Browser Admin.")
            expect(page.locator("[data-closed-badge]")).to_be_visible()

            # The owner reopens it.
            page.locator("[data-month-reopen]").click()
            expect(dialog).to_contain_text("Reopen March 2025?")
            page.get_by_role("button", name="Continue").click()
            page.wait_for_url(f"{base}/m/2025-03?notice=reopened")
            expect(page.locator("[data-closed-banner]")).to_have_count(0)
            expect(page.locator("[data-notice]")).to_have_text("Month reopened.")

            assert page.evaluate("window.__cspViolations") == []
            assert not [e for e in console_errors if "Content Security Policy" in e]
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
