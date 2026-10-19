"""The month summary in Chromium (F2.4): actual, difference and KPIs refresh without a reload
after a planned value or an entry is saved, the sort toggle reorders the rows and is kept, and
there are no CSP violations. The test's categories are created directly ("Golden ..."); the
suggested set that setup gives every new workspace stays in the summary with nothing in it."""

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
from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.db.models.identity import Workspace
from assetboard.db.models.ledger import Category

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
MONTH = "2025-03"


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


def _order(page: Page) -> list[str]:
    """The expense rows of this test's categories, in page order (the seeded ones are there
    too, with nothing in them)."""
    names = page.locator(
        "#month-summary [data-plan-group='expense'] [data-plan-category]"
    ).all_text_contents()
    return [name for name in names if name.startswith("Golden ")]


def test_f24_summary_in_a_browser(tmp_path: Path) -> None:
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
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_label("Workspace name").fill("Browser Home")
            page.get_by_role("button", name="Create administrator").click()
            page.wait_for_url(f"{base}/")
            with app.state.session_factory() as session, session.begin():
                workspace = session.scalars(select(Workspace)).one()
                for order, (name, kind) in enumerate(
                    [("Golden Food", "expense"), ("Golden Travel", "expense"),
                     ("Golden Salary", "income")]
                ):  # fmt: skip
                    session.add(
                        Category(
                            workspace_id=workspace.id, name=name, name_key=name.casefold(),
                            kind=kind, treatment="regular", color="#16a34a", sort_order=order,
                        )
                    )  # fmt: skip

            page.goto(f"{base}/m/{MONTH}")
            food_row = page.locator("#month-summary tr[data-plan-row]", has_text="Golden Food")
            travel_row = page.locator("#month-summary tr[data-plan-row]", has_text="Golden Travel")
            kpis = page.locator("#summary-kpis [data-kpi]")
            expect(kpis).to_have_text(
                ["€0.00", "€0.00", "€0.00", "€0.00", "—Not available: nothing earned"]
            )

            # A planned value updates the difference, the totals and the bar text at once.
            page.get_by_label("Planned for Golden Food").fill("550")
            page.keyboard.press("Tab")
            expect(food_row.locator("[data-difference]")).to_contain_text("+€550.00")
            expect(page.locator("#summary-total-expense")).to_contain_text("€550.00")
            expect(page.locator("[data-summary-bar='expense']")).to_contain_text(
                "Spent €0.00 of €550.00 planned"
            )

            # An entry updates actual, difference and the KPIs without a reload.
            page.locator("#entry-expense-day").fill("3")
            page.locator("#entry-expense-amount").fill("50")
            page.locator("#entry-expense-category").select_option(label="Golden Food")
            page.locator("#entry-expense-amount").press("Enter")
            expect(food_row.locator("[data-actual]")).to_have_text("€50.00")
            expect(food_row.locator("[data-difference]")).to_contain_text("+€500.00")
            expect(kpis.nth(1)).to_have_text("€50.00")
            page.locator("#entry-income-day").fill("1")
            page.locator("#entry-income-amount").fill("1000")
            page.locator("#entry-income-category").select_option(label="Golden Salary")
            page.locator("#entry-income-amount").press("Enter")
            expect(kpis).to_have_text(["€1,000.00", "€50.00", "€0.00", "€950.00", "95.0%"])

            # Largest first by default; "Your order" keeps the categories' order, and is kept.
            page.locator("#entry-expense-day").fill("4")
            page.locator("#entry-expense-amount").fill("400")
            page.locator("#entry-expense-category").select_option(label="Golden Travel")
            page.locator("#entry-expense-amount").press("Enter")
            expect(travel_row.locator("[data-actual]")).to_have_text("€400.00")
            assert _order(page) == ["Golden Travel", "Golden Food"]
            page.get_by_role("button", name="Your order").click()
            expect(page.get_by_role("button", name="Your order")).to_have_attribute(
                "aria-pressed", "true"
            )
            # The focus stays on the pressed button's replacement (stable ids across the swap).
            expect(page.locator("#summary-sort-manual")).to_be_focused()
            assert _order(page) == ["Golden Food", "Golden Travel"]
            page.reload()
            assert _order(page) == ["Golden Food", "Golden Travel"]
            page.get_by_role("button", name="Largest first").click()
            expect(page.get_by_role("button", name="Largest first")).to_have_attribute(
                "aria-pressed", "true"
            )
            assert page.evaluate("document.activeElement.id") == "summary-sort-actual"
            assert _order(page) == ["Golden Travel", "Golden Food"]

            assert page.evaluate("window.__cspViolations") == []
            assert not [e for e in console_errors if "Content Security Policy" in e]
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
