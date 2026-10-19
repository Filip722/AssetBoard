"""Self-service workspaces in Chromium (pre-release A): an account without a workspace creates
one from the "No workspace" page, and an administrator turns creation off. No CSP violations."""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.db.models.identity import User
from assetboard.services.auth import argon2_hash, run_initial_setup

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium tests",
)

PASSWORD = "Correct Horse Battery!"
VIOLATION_RECORDER = """
window.__cspViolations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__cspViolations.push(event.violatedDirective + " " + (event.blockedURI || "inline"));
});
"""


def _sign_in(page: Page, base: str, email: str) -> None:
    page.goto(f"{base}/login", wait_until="networkidle")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()


def test_self_service_workspaces_in_a_browser(tmp_path: Path) -> None:
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
        run_initial_setup(
            app.state.session_factory,
            email="admin@example.com",
            display_name="Browser Admin",
            password=PASSWORD,
            workspace_name="Browser Home",
            settings=app.state.settings,
            clock=app.state.clock,
        )
        with app.state.session_factory.begin() as session:
            session.add(
                User(
                    email="bea@example.com", display_name="Bea", password_hash=argon2_hash(PASSWORD)
                )
            )
        base = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()

            # Bea has no workspace: the 403 page offers to create one.
            bea = browser.new_context(color_scheme="light")
            bea.add_init_script(VIOLATION_RECORDER)
            page = bea.new_page()
            _sign_in(page, base, "bea@example.com")
            expect(page.locator("[data-no-workspace]")).to_be_visible()
            page.locator("[data-create-workspace]").click()
            page.wait_for_url(f"{base}/workspaces/new")
            expect(page.get_by_label("Workspace name")).to_have_value("Bea")
            page.get_by_label("Workspace name").fill("Bea's budget")
            page.get_by_label("Base currency").select_option("PLN")
            page.get_by_role("button", name="Create workspace").click()
            page.wait_for_url(f"{base}/")
            expect(page.locator("[data-current-workspace]")).to_have_text("Bea's budget")
            page.locator("[data-categories-link]").click()
            page.wait_for_url(f"{base}/categories")
            expense = page.locator('[data-section="expense"] [data-category-name]')
            expect(expense.first).to_have_text("Food")
            assert page.evaluate("window.__cspViolations") == []

            # The administrator turns self-service creation off.
            admin = browser.new_context(color_scheme="light")
            admin.add_init_script(VIOLATION_RECORDER)
            admin_page = admin.new_page()
            _sign_in(admin_page, base, "admin@example.com")
            admin_page.wait_for_url(f"{base}/")
            admin_page.goto(f"{base}/admin/settings", wait_until="networkidle")
            admin_page.get_by_label("Who can create workspaces").select_option("off")
            admin_page.get_by_label("Your password").fill(PASSWORD)
            admin_page.get_by_role("button", name="Save settings").click()
            expect(admin_page.locator("[data-admin-notice]")).to_have_text(
                "Instance settings saved."
            )
            assert admin_page.evaluate("window.__cspViolations") == []

            # Bea no longer sees the offer.
            page.goto(f"{base}/", wait_until="networkidle")
            expect(page.locator("[data-new-workspace-link]")).to_have_count(0)
            assert page.evaluate("window.__cspViolations") == []
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
