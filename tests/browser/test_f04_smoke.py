from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import uvicorn
from fastapi.responses import HTMLResponse, Response
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.db.models.identity import Membership, User, Workspace
from assetboard.web.rendering import templates_for

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium smoke test",
)


def _toast_html(message: str, undo_url: str | None, undo_label: str) -> str:
    template = templates_for("en").env.from_string(
        '{% from "macros/components.html" import toast %}{{ toast(message, url, label) }}'
    )
    return template.render(message=message, url=undo_url, label=undo_label)


def test_f04_browser_shell_smoke(tmp_path: Path) -> None:
    request_log: list[str] = []
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="browser-test-key",
        )
    )

    @app.get("/_smoke/htmx")
    async def htmx_confirmed() -> HTMLResponse:
        request_log.append("htmx")
        return HTMLResponse("HTMX confirmed")

    @app.post("/_smoke/htmx-form")
    async def htmx_form_confirmed() -> HTMLResponse:
        request_log.append("htmx-form")
        return HTMLResponse("HTMX form confirmed")

    @app.post("/_smoke/plain")
    async def plain_confirmed() -> HTMLResponse:
        request_log.append("plain")
        return HTMLResponse("<!doctype html><html><body>Plain form confirmed</body></html>")

    @app.get("/_smoke/toast")
    async def toast_response() -> HTMLResponse:
        return HTMLResponse(_toast_html("Smoke toast", "/_smoke/undo", "Undo"))

    @app.get("/_smoke/short-toast")
    async def short_toast_response() -> HTMLResponse:
        return HTMLResponse(_toast_html("Short smoke toast", None, ""))

    @app.post("/_smoke/undo")
    async def undo_toast() -> Response:
        request_log.append("undo")
        return Response(status_code=204)

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    server_thread.start()
    startup_deadline = time.monotonic() + 10
    while not server.started and server_thread.is_alive() and time.monotonic() < startup_deadline:
        threading.Event().wait(0.01)

    try:
        assert server.started
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(color_scheme="light")
            page = context.new_page()
            base = f"http://127.0.0.1:{port}"
            page.goto(f"{base}/", wait_until="networkidle")

            # First run: every page leads to setup, which signs the administrator in.
            assert page.url == f"{base}/setup"
            page.get_by_label("Setup token").fill(app.state.setup_token)
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Display name").fill("Browser Admin")
            page.get_by_label("Password").fill("Correct Horse Battery!")
            page.get_by_label("Workspace name").fill("Browser Home")
            page.get_by_role("button", name="Create administrator").click()
            page.wait_for_url(f"{base}/")
            expect(page.get_by_text("Browser Admin")).to_be_visible()
            expect(page.locator("[data-current-workspace]")).to_have_text("Browser Home")

            # HTMX sends the CSRF token in a header; a bad token becomes an error toast.
            page.goto(f"{base}/account/password", wait_until="networkidle")
            page.evaluate(
                "document.querySelector('meta[name=\"csrf-token\"]').content = 'tampered'"
            )
            page.get_by_role("button", name="Sign out other devices").click()
            csrf_toast = page.locator('#toast-container [data-toast][role="alert"]')
            expect(csrf_toast).to_contain_text("This form has expired")
            page.reload(wait_until="networkidle")
            page.get_by_role("button", name="Sign out other devices").click()
            expect(page.locator("#sessions-status")).to_contain_text(
                "Other sessions signed out: 0."
            )

            # Sign out through the account menu, then sign in again.
            page.locator("details summary").click()
            page.get_by_role("button", name="Sign out", exact=True).click()
            page.wait_for_url(f"{base}/login")
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Password").fill("Correct Horse Battery!")
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url(f"{base}/")

            # Removing a member asks for confirmation through the CSP-safe data-confirm handler.
            with app.state.session_factory.begin() as session:
                bea = User(email="bea@example.com", display_name="Bea")
                session.add(bea)
                session.flush()
                home = session.scalars(select(Workspace.id)).one()
                session.add(Membership(workspace_id=home, user_id=bea.id, role="viewer"))
            members_response = page.goto(f"{base}/workspace/members", wait_until="networkidle")
            assert members_response is not None
            csp = members_response.headers["content-security-policy"]
            assert "script-src 'self'" in csp and "unsafe-inline" not in csp
            bea_row = page.locator("li[data-member]", has_text="bea@example.com")
            bea_row.get_by_role("button", name="Remove").click()
            confirm = page.locator("#confirm-dialog")
            expect(confirm).to_be_visible()
            expect(confirm).to_contain_text("Remove Bea from this workspace?")
            page.keyboard.press("Escape")
            expect(confirm).to_be_hidden()
            expect(bea_row).to_be_visible()
            bea_row.get_by_role("button", name="Remove").click()
            page.get_by_role("button", name="Continue").click()
            page.wait_for_url(f"{base}/workspace/members")
            expect(page.locator("li[data-member]", has_text="bea@example.com")).to_have_count(0)

            # Invitation links in a real browser: the forms must pass the Origin check, so the
            # invite pages may not use "no-referrer" (it makes browsers send Origin: null).
            page.get_by_label("Email (optional)").fill("")
            page.get_by_role("button", name="Create invitation link").click()
            # The link is built from BASE_URL; open its path on this test server.
            link = base + urlsplit(page.locator("#invitation-link").input_value()).path
            assert "/invite/" in link
            guest = browser.new_context(color_scheme="light")
            guest_page = guest.new_page()
            invite_response = guest_page.goto(link, wait_until="networkidle")
            assert invite_response is not None
            assert invite_response.headers["referrer-policy"] == "same-origin"
            guest_page.get_by_label("Email").fill("guest@example.com")
            guest_page.get_by_label("Display name").fill("Guest")
            guest_page.get_by_label("Password").fill("Another Correct Horse!")
            guest_page.get_by_role("button", name="Create account and join").click()
            guest_page.wait_for_url(f"{base}/")
            expect(guest_page.locator("[data-current-workspace]")).to_have_text("Browser Home")
            guest.close()
            # A signed-in member accepting a second link: told they already belong.
            page.goto(f"{base}/workspace/members", wait_until="networkidle")
            page.get_by_role("button", name="Create invitation link").click()
            second_link = base + urlsplit(page.locator("#invitation-link").input_value()).path
            page.goto(second_link, wait_until="networkidle")
            page.get_by_role("button", name="Accept invitation").click()
            expect(page.get_by_text("You are already a member of this workspace.")).to_be_visible()
            page.goto(f"{base}/", wait_until="networkidle")

            page.get_by_role("button", name="Toggle dark mode").click()
            assert page.locator("html").evaluate("element => element.classList.contains('dark')")
            assert page.evaluate("localStorage.getItem('theme')") == "dark"
            page.reload(wait_until="networkidle")
            assert page.locator("html").evaluate("element => element.classList.contains('dark')")

            page.evaluate(
                """() => {
                  const mount = document.createElement('section');
                  mount.innerHTML = `
                    <button id="smoke-hx-confirm" hx-get="/_smoke/htmx"
                      hx-target="#smoke-result" data-confirm="Proceed with HTMX?">HTMX</button>
                    <div id="smoke-result"></div>
                    <form id="smoke-hx-form" hx-post="/_smoke/htmx-form"
                      hx-target="#smoke-form-result" data-confirm="Proceed with HX form?">
                      <button id="smoke-hx-submit" type="submit">Submit HX form</button>
                    </form>
                    <div id="smoke-form-result"></div>
                    <form id="smoke-plain-form" method="post" action="/_smoke/plain"
                      target="_blank" data-confirm="Proceed with plain form?">
                      <button id="smoke-plain-submit" type="submit">Submit plain form</button>
                    </form>
                    <button id="smoke-toast-trigger" hx-get="/_smoke/toast">Toast</button>
                    <button id="smoke-short-toast-trigger"
                      hx-get="/_smoke/short-toast">Short toast</button>
                    <button id="smoke-error-trigger" hx-get="/_smoke/missing">Error</button>`;
                  document.body.append(mount);
                  window.htmx.process(mount);
                }"""
            )

            dialog = page.locator("#confirm-dialog")
            page.locator("#smoke-hx-confirm").click()
            expect(dialog).to_be_visible()
            assert dialog.evaluate("element => element.returnValue") == ""
            page.keyboard.press("Escape")
            expect(dialog).to_be_hidden()
            assert "htmx" not in request_log

            page.locator("#smoke-hx-confirm").click()
            expect(dialog).to_be_visible()
            page.get_by_role("button", name="Continue").click()
            expect(page.locator("#smoke-result")).to_have_text("HTMX confirmed")
            assert request_log.count("htmx") == 1

            page.locator("#smoke-hx-submit").click()
            expect(dialog).to_be_visible()
            page.get_by_role("button", name="Continue").click()
            expect(page.locator("#smoke-form-result")).to_have_text("HTMX form confirmed")
            assert request_log.count("htmx-form") == 1

            page.locator("#smoke-plain-submit").click()
            expect(dialog).to_be_visible()
            page.keyboard.press("Escape")
            expect(dialog).to_be_hidden()
            assert "plain" not in request_log

            page.locator("#smoke-plain-submit").click()
            expect(dialog).to_be_visible()
            with context.expect_page() as plain_page_info:
                page.get_by_role("button", name="Continue").click()
            plain_page = plain_page_info.value
            expect(plain_page.locator("body")).to_have_text("Plain form confirmed")
            assert request_log.count("plain") == 1

            page.evaluate(
                """() => {
                  const realSetTimeout = window.setTimeout.bind(window);
                  const realClearTimeout = window.clearTimeout.bind(window);
                  window.__toastTimerCalls = [];
                  window.__toastTimerClears = [];
                  window.setTimeout = (callback, delay, ...args) => {
                    if (delay >= 4000 && delay <= 10000) {
                      const id = -window.__toastTimerCalls.length - 1;
                      window.__toastTimerCalls.push({id, callback, delay});
                      return id;
                    }
                    return realSetTimeout(callback, delay, ...args);
                  };
                  window.clearTimeout = id => {
                    if (id < 0) {
                      window.__toastTimerClears.push(id);
                      return;
                    }
                    realClearTimeout(id);
                  };
                }"""
            )
            page.locator("#smoke-toast-trigger").click()
            toast = page.locator('#toast-container [data-toast]:has-text("Smoke toast")')
            expect(toast).to_be_visible()
            timer_delays = page.evaluate("window.__toastTimerCalls.map(timer => timer.delay)")
            assert timer_delays == [10000]

            with page.expect_response(
                lambda response: response.url.endswith("/_smoke/undo")
            ) as undo:
                page.get_by_role("button", name="Undo").click()
            assert undo.value.status == 204
            assert request_log.count("undo") == 1
            page.mouse.move(1, 1)
            assert page.evaluate("window.__toastTimerCalls.length") == 1
            assert page.evaluate("window.__toastTimerClears.length") == 1
            page.locator("#smoke-error-trigger").focus()
            resumed_delay = page.evaluate("window.__toastTimerCalls.at(-1).delay")
            assert page.evaluate("window.__toastTimerCalls.length") == 2
            assert 0 < resumed_delay <= 10000

            page.locator("#smoke-short-toast-trigger").click()
            short_toast = page.locator(
                '#toast-container [data-toast]:has-text("Short smoke toast")'
            )
            expect(short_toast).to_be_visible()
            assert page.evaluate("window.__toastTimerCalls.at(-1).delay") == 5000

            page.locator("#smoke-error-trigger").click()
            error_toast = page.locator('#toast-container [data-toast][role="alert"]')
            expect(error_toast).to_contain_text("Page not found")
            expect(error_toast).to_contain_text("could not be found")
            assert page.evaluate("window.__toastTimerCalls.at(-1).delay") == 5000
            assert "<!doctype html>" not in page.locator("#toast-container").inner_html().lower()

            context.close()
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
        listener.close()
