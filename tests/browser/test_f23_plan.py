"""Planned values on the month page in Chromium (F2.3 part B): typing a plan with Enter and Tab,
Esc, a refused value, the copy banner, a half-typed planned value surviving a transaction write
that refreshes the summary, the automatic copy setting, and no CSP violations."""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from assetboard.app import create_app
from assetboard.config import Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium tests",
)

VIOLATION_RECORDER = """
window.__cspViolations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__cspViolations.push(event.violatedDirective + " " + (event.blockedURI || "inline"));
});
"""
PASSWORD = "Correct Horse Battery!"
FEBRUARY, MARCH = "2025-02", "2025-03"  # past months: the banner, never the automatic copy
FUTURE, NEXT = "2199-11", "2199-12"  # future months: the automatic copy applies


class SlowPosts:
    """Delays every POST by ``delay`` seconds (network latency, without blocking the test)."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.delay = 0.0
        self.plan_delay = 0.0  # planned-value saves only

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["method"] == "POST":
            delay = self.plan_delay if "/plan/" in scope["path"] else self.delay
            if delay:
                await asyncio.sleep(delay)
        await self.app(scope, receive, send)


def test_f23_plan_in_a_browser(tmp_path: Path) -> None:
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
    slow = SlowPosts(app)
    server = uvicorn.Server(uvicorn.Config(slow, log_level="error", access_log=False))
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
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_label("Workspace name").fill("Browser Home")
            page.get_by_role("button", name="Create administrator").click()
            page.wait_for_url(f"{base}/")
            page.goto(f"{base}/categories")
            page.locator("[data-seed]").click()
            expect(page.locator("li[data-category]").first).to_be_visible()

            # Type a plan: Enter moves to the next cell and saves the one left behind.
            page.goto(f"{base}/m/{FEBRUARY}")
            food = page.get_by_label("Planned for Food")
            eating = page.get_by_label("Planned for Eating out")
            status = page.locator("#plan-status")
            food.click()
            page.keyboard.type("550")
            page.keyboard.press("Enter")
            expect(eating).to_be_focused()
            expect(status).to_have_text("Planned value saved.")
            expect(food).to_have_value("550.00")
            page.keyboard.type("120.5")
            page.keyboard.press("Tab")  # leaving the field saves too
            expect(eating).to_have_value("120.50")

            # Esc restores the saved value; a refused value keeps its text and message.
            food.click()
            page.keyboard.press("Control+A")
            page.keyboard.type("999")
            page.keyboard.press("Escape")
            expect(food).to_have_value("550.00")
            page.keyboard.press("Control+A")
            page.keyboard.type("-1")
            page.keyboard.press("Enter")
            expect(page.locator("[data-plan-row] [data-field-error]")).to_have_text(
                "A planned value cannot be negative."
            )
            expect(food).to_have_attribute("aria-invalid", "true")
            expect(eating).to_be_focused()  # the error does not pull the focus back
            food.click()
            page.keyboard.press("Escape")
            expect(food).to_have_value("550.00")
            expect(page.locator("[data-plan-row] [data-field-error]")).to_have_count(0)
            page.reload()
            expect(food).to_have_value("550.00")
            expect(eating).to_have_value("120.50")

            # The next month offers the plan; one click copies it and focuses the first cell.
            page.goto(f"{base}/m/{MARCH}")
            banner = page.locator("[data-plan-banner]")
            expect(banner).to_contain_text("This month has no plan yet.")
            page.get_by_role("button", name="Copy plan from February 2025").click()
            expect(page.locator("[data-plan-copied]")).to_have_text(
                "Copied 2 planned values from February 2025."
            )
            expect(banner).to_have_count(0)
            expect(food).to_have_value("550.00")
            expect(food).to_be_focused()

            # A transaction write refreshes the summary out of band: a planned value being
            # typed keeps its text and focus, and is saved when the field is left.
            slow.delay = 0.8
            page.locator("#entry-expense-day").fill("3")
            page.locator("#entry-expense-amount").fill("12.50")
            page.locator("#entry-expense-category").select_option(label="Food")
            page.locator("#entry-expense-amount").press("Enter")
            food.click()
            page.keyboard.press("Control+A")
            page.keyboard.type("42")
            expect(page.locator("#ledger-expense [data-amount]")).to_have_text(["€12.50"])
            expect(food).to_be_focused()
            expect(food).to_have_value("42")
            slow.delay = 0
            page.keyboard.press("Tab")
            expect(status).to_have_text("Planned value saved.")
            page.reload()
            expect(food).to_have_value("42.00")

            # A cell's save that finishes after a transaction write replaced the summary lands
            # in the new block's row (not the detached old one).
            slow.plan_delay = 1.0
            food.fill("77")
            page.keyboard.press("Tab")  # the save is on its way, slowly
            page.locator("#entry-expense-day").fill("4")
            page.locator("#entry-expense-amount").fill("1")
            page.locator("#entry-expense-category").select_option(label="Food")
            page.locator("#entry-expense-amount").press("Enter")
            expect(page.locator("#ledger-expense [data-amount]")).to_have_count(2)
            expect(food).to_have_value("77.00")
            expect(food).to_have_attribute("data-saved-value", "77.00")
            slow.plan_delay = 0
            page.reload()
            expect(food).to_have_value("77.00")

            # The workspace setting copies the plan when a later month is first opened.
            page.goto(f"{base}/workspace/settings")
            page.get_by_label(
                "Copy the previous month's plan when a month is opened for the first time"
            ).check()
            page.get_by_role("button", name="Save settings").click()
            page.wait_for_url(f"{base}/workspace/settings?saved=1")
            page.goto(f"{base}/m/{FUTURE}")
            food.fill("10")
            page.keyboard.press("Tab")
            expect(status).to_have_text("Planned value saved.")
            page.goto(f"{base}/m/{NEXT}")
            expect(page.locator("[data-plan-copied]")).to_have_text(
                "1 planned value was copied from November 2199 (workspace setting)."
            )
            expect(food).to_have_value("10.00")

            assert page.evaluate("window.__cspViolations") == []
            assert not [e for e in console_errors if "Content Security Policy" in e]
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
