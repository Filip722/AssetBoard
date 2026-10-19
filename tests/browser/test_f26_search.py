"""The transaction list in Chromium (F2.6): filtering, pages, the CSV download with options,
bulk recategorize through its confirmation page, a closed-month refusal, a viewer, and no CSP
violations."""

from __future__ import annotations

import os
import re
import socket
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.db.models.identity import Membership, User, Workspace
from assetboard.db.models.ledger import Category, MonthState, Transaction
from assetboard.services.auth import argon2_hash

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


def _descriptions(page: Page) -> list[str]:
    return page.locator("[data-results] [data-description]").all_text_contents()


def _sign_in(page: Page, base: str, email: str) -> None:
    page.goto(f"{base}/login")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(f"{base}/")


def _seed_rows(app: object) -> None:
    """55 synthetic expenses in two months and one income, entered in a known order."""
    factory = app.state.session_factory  # type: ignore[attr-defined]
    with factory() as session, session.begin():
        workspace = session.scalars(select(Workspace)).one()
        categories = {
            (c.kind, c.name): c.id
            for c in session.scalars(select(Category).where(Category.workspace_id == workspace.id))
        }
        stamp = datetime(2025, 3, 1, tzinfo=UTC)
        rows = [
            ("expense", "Food", date(2025, 3, 7), 1250, "Żabka"),
            ("expense", "Food", date(2025, 4, 2), 420, "Bakery"),
            ("income", "Salary", date(2025, 3, 31), 340000, "March pay"),
        ]
        rows += [
            ("expense", "Transport", date(2025, 4, 1 + n % 28), 100 + n, f"Ticket {n:02d}")
            for n in range(53)
        ]
        for n, (kind, name, day, minor, text) in enumerate(rows):
            session.add(
                Transaction(
                    workspace_id=workspace.id,
                    direction=kind,
                    date=day,
                    amount_minor=minor,
                    currency="EUR",
                    category_id=categories[(kind, name)],
                    description=text,
                    description_key=text.casefold(),
                    source="manual",
                    created_by_user_id=None,
                    created_at=stamp + timedelta(seconds=n),
                    updated_at=stamp,
                )  # fmt: skip
            )


def test_f26_search_export_and_bulk_change_in_a_browser(tmp_path: Path) -> None:
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
            context = browser.new_context(color_scheme="light", accept_downloads=True)
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
            _seed_rows(app)

            # The navigation leads to the list: newest first, 50 per page.
            page.locator("[data-transactions-link]").first.click()
            page.wait_for_url(f"{base}/transactions")
            expect(page.locator("[data-result-count]")).to_have_text("1–50 of 56 entries")
            expect(page.locator("[data-page-number]")).to_have_text("Page 1 of 2")
            page.locator("[data-next]").click()
            expect(page.locator("[data-result-count]")).to_have_text("51–56 of 56 entries")
            assert _descriptions(page)[-2:] == ["March pay", "Żabka"]

            # Filter by text (case and Polish letters fold) and by kind.
            page.goto(f"{base}/transactions")
            page.get_by_label("Description contains").fill("ŻAB")
            page.get_by_role("button", name="Show entries").click()
            expect(page).to_have_url(re.compile(r"/transactions\?.*q=%C5%BBAB"))  # bookmarkable
            assert _descriptions(page) == ["Żabka"]
            page.get_by_label("Description contains").fill("")
            page.get_by_label("Kind", exact=True).select_option("income")
            page.get_by_role("button", name="Show entries").click()
            assert _descriptions(page) == ["March pay"]
            # Amount range in the user's format, smallest first.
            page.goto(f"{base}/transactions")
            page.get_by_label("Smallest amount").fill("4.2")
            page.get_by_label("Largest amount").fill("12,5")
            page.get_by_label("Order", exact=True).select_option("amount")
            page.get_by_role("button", name="Show entries").click()
            assert _descriptions(page) == ["Bakery", "Żabka"]
            # An invalid filter is marked on its field.
            page.get_by_label("Largest amount").fill("abc")
            page.get_by_role("button", name="Show entries").click()
            expect(page.locator("#filter-amount_max-error")).to_have_text("Enter a valid amount.")
            expect(page.get_by_label("Largest amount")).to_have_attribute("aria-invalid", "true")

            # Export the filtered rows with semicolons and a decimal comma.
            page.goto(f"{base}/transactions?min=4.2&max=12.5")
            page.get_by_label("Delimiter").select_option("semicolon")
            page.get_by_label("Decimal separator").select_option("comma")
            with page.expect_download() as caught:
                page.get_by_role("button", name="Download CSV").click()
            download = caught.value
            assert download.suggested_filename.startswith("assetboard-transactions-")
            data = Path(download.path()).read_bytes().decode("utf-8")
            lines = data.split("\r\n")
            assert lines[0] == "date;direction;amount;currency;category;description;category_id;id"
            assert lines[1].startswith("2025-03-07;expense;12,50;EUR;Food;Żabka;")
            assert lines[2].startswith("2025-04-02;expense;4,20;EUR;Food;Bakery;")
            assert lines[3:] == [""]
            # A decimal comma with commas is refused on the page.
            page.goto(f"{base}/transactions")
            page.get_by_label("Decimal separator").select_option("comma")
            page.get_by_role("button", name="Download CSV").click()
            expect(page.locator("[data-export-error]")).to_contain_text(
                "A decimal comma needs the semicolon delimiter"
            )

            # Bulk change: tick two entries, review, confirm.
            page.goto(f"{base}/transactions?min=4.2&max=12.5")
            page.get_by_label("Select the entry of Mar 7, 2025, €12.50").check()
            page.get_by_label("Select the entry of Apr 2, 2025, €4.20").check()
            page.get_by_label("New category").select_option(label="Eating out")
            page.get_by_role("button", name="Change the ticked entries").click()
            expect(page.locator("#confirm-heading")).to_have_text("Move 2 entries to Eating out?")
            expect(page.locator("[data-months]")).to_have_text("March 2025, April 2025")
            page.get_by_role("button", name="Change category").click()
            expect(page.locator("[data-notice]")).to_have_text("Category changed for 2 entries.")
            expect(page.locator("[data-results] [data-category-name]")).to_have_text(
                ["Eating out", "Eating out"]
            )

            # Changing every matching entry is refused as a whole when a month is closed
            # between the review and the confirmation.
            page.goto(f"{base}/transactions?direction=expense&q=a")
            page.get_by_label("New category").select_option(label="Food")
            page.get_by_role("button", name="Change all 2 matching entries").click()
            expect(page.locator("#confirm-heading")).to_have_text("Move 2 entries to Food?")
            with app.state.session_factory() as session, session.begin():
                workspace = session.scalars(select(Workspace)).one()
                session.add(
                    MonthState(
                        workspace_id=workspace.id,
                        month=date(2025, 3, 1),
                        closed_at=datetime(2025, 4, 1, tzinfo=UTC),
                    )  # fmt: skip
                )
            page.get_by_role("button", name="Change category").click()
            expect(page.locator("[data-bulk-error]")).to_contain_text(
                "Nothing was changed: some entries are in closed months (March 2025)."
            )
            expect(page.locator("[data-results] [data-category-name]")).to_have_text(
                ["Eating out", "Eating out"]
            )

            assert page.evaluate("window.__cspViolations") == []
            assert not [e for e in console_errors if "Content Security Policy" in e]

            # A viewer lists and exports but has no bulk controls.
            with app.state.session_factory() as session, session.begin():
                workspace = session.scalars(select(Workspace)).one()
                viewer = User(
                    email="viewer@example.com",
                    display_name="Viewer",
                    password_hash=argon2_hash(PASSWORD),
                )
                session.add(viewer)
                session.flush()
                session.add(Membership(workspace_id=workspace.id, user_id=viewer.id, role="viewer"))
            viewer_page = browser.new_context(color_scheme="light").new_page()
            _sign_in(viewer_page, base, "viewer@example.com")
            viewer_page.goto(f"{base}/transactions")
            expect(viewer_page.locator("[data-result-count]")).to_have_text("1–50 of 56 entries")
            expect(viewer_page.locator("[data-bulk], input[name=ids]")).to_have_count(0)
            with viewer_page.expect_download() as caught:
                viewer_page.get_by_role("button", name="Download CSV").click()
            assert Path(caught.value.path()).read_bytes().count(b"\r\n") == 57
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
