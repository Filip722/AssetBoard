"""Registration and change-password pages; no secret ever reaches the logs."""

from __future__ import annotations

import io
import logging
import re
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from assetboard.db.models.identity import Membership, User

if TYPE_CHECKING:
    from conftest import CountingHasher, WebHarness

NEW_PASSWORD = "Another Strong Password"


def _register(client: TestClient, web: WebHarness, email: str, password: str = NEW_PASSWORD):
    data = {
        "csrf_token": web.csrf(client.get("/register").text),
        "email": email,
        "display_name": "Newcomer",
        "password": password,
        "workspace_name": "Budget",
    }
    return client.post("/register", data=data, follow_redirects=False)


def test_registration_signs_the_new_user_in(web: WebHarness) -> None:
    app = web.app(allow_registration=True)
    with TestClient(app, client=("192.0.2.60", 1000)) as client:
        web.complete_setup(app)
        response = _register(client, web, "New@Example.com")
        assert (response.status_code, response.headers["location"]) == (303, "/")
        cookies = response.headers.get_list("set-cookie")
        assert any(c.startswith("assetboard_session=") for c in cookies)
        assert any(c.startswith("assetboard_csrf=") and "Max-Age=0" in c for c in cookies)
        home = client.get("/")
        assert home.status_code == 200 and "Newcomer" in home.text
    with app.state.session_factory() as session:
        user = session.scalars(select(User).where(User.email == "new@example.com")).one()
        membership = session.scalars(select(Membership).where(Membership.user_id == user.id)).one()
    assert (user.is_instance_admin, membership.role) == (False, "owner")


def test_registration_outcomes(web: WebHarness) -> None:
    app = web.app(allow_registration=True)
    with TestClient(app, client=("192.0.2.61", 1000)) as client:
        web.complete_setup(app)
        existing = _register(client, web, web.admin_email)
        weak = _register(client, web, "weak@example.com", "qwertyuiop")
        for index in range(3):
            _register(client, web, f"invalid-{index}")
        throttled = _register(client, web, "late@example.com")
    assert existing.status_code == 400 and "cannot be registered" in existing.text
    assert weak.status_code == 400 and "too common" in weak.text
    assert throttled.status_code == 429 and "Too many registration attempts" in throttled.text


def test_oversized_registration_fields_are_refused_before_hashing(
    web: WebHarness, argon2_calls: CountingHasher
) -> None:
    app = web.app(allow_registration=True)
    with TestClient(app, client=("192.0.2.62", 1000)) as client:
        web.complete_setup(app)
        before = argon2_calls.hashes
        response = _register(client, web, "big@example.com", "p" * 1025)
    assert response.status_code == 400 and "This value is too long." in response.text
    assert argon2_calls.hashes == before


def _change(
    client: TestClient, web: WebHarness, current: str, new: str, confirm: str | None = None
):
    return client.post(
        "/account/password",
        data={
            "csrf_token": web.page_token(client, "/account/password"),
            "current_password": current,
            "new_password": new,
            "confirm_password": new if confirm is None else confirm,
        },
    )


def test_change_password_page_flow(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app, client=("192.0.2.63", 1000)) as client, TestClient(app) as other:
        web.complete_setup(app)
        web.sign_in(client)
        web.sign_in(other)
        mismatch = _change(client, web, web.admin_password, NEW_PASSWORD, "something else")
        weak = _change(client, web, web.admin_password, "qwertyuiop")
        wrong = _change(client, web, "wrong current", NEW_PASSWORD)
        changed = _change(client, web, web.admin_password, NEW_PASSWORD)
        assert other.get("/", follow_redirects=False).status_code == 303  # signed out
        assert client.get("/").status_code == 200  # this device stays signed in
    assert mismatch.status_code == 400 and "do not match" in mismatch.text
    assert weak.status_code == 400 and "too common" in weak.text
    assert wrong.status_code == 400 and "current password is not correct" in wrong.text
    assert changed.status_code == 200 and "Your password was changed." in changed.text


def test_change_password_is_throttled(signed_in: TestClient, web: WebHarness) -> None:
    for _ in range(6):
        _change(signed_in, web, "wrong current", NEW_PASSWORD)
    throttled = _change(signed_in, web, web.admin_password, NEW_PASSWORD)
    assert throttled.status_code == 429 and "Too many attempts" in throttled.text


def test_no_password_session_id_or_csrf_token_is_logged(
    web: WebHarness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    app = web.app(allow_registration=True)
    # The app logger does not propagate to the root logger; capture it directly as well.
    app_log = io.StringIO()
    handler = logging.StreamHandler(app_log)
    handler.setLevel(logging.DEBUG)
    app.state.logger.addHandler(handler)
    app.state.logger.setLevel(logging.DEBUG)
    secrets: list[str] = [web.admin_password, NEW_PASSWORD, "Registrant Strong Pw"]
    with TestClient(app, client=("192.0.2.64", 1000)) as client:
        setup_token = app.state.setup_token
        page = client.get("/setup")
        secrets.append(web.csrf(page.text))
        response = client.post(
            "/setup",
            data={
                "csrf_token": web.csrf(page.text),
                "setup_token": setup_token,
                "email": web.admin_email,
                "display_name": "Admin",
                "password": web.admin_password,
            },
            follow_redirects=False,
        )
        secrets.append(response.cookies["assetboard_session"])
        secrets.append(web.page_token(client, "/account/password"))
        _change(client, web, web.admin_password, NEW_PASSWORD)
        client.post("/logout", data={"csrf_token": web.page_token(client)})
        response = client.post(
            "/login",
            data={
                "csrf_token": web.csrf(client.get("/login").text),
                "email": web.admin_email,
                "password": NEW_PASSWORD,
            },
            follow_redirects=False,
        )
        secrets.append(response.cookies["assetboard_session"])
        client.post("/logout", data={"csrf_token": "forged-token"})  # CSRF failure path
        client.post("/logout", data={"csrf_token": web.page_token(client)})
        _register(client, web, "reg@example.com", "Registrant Strong Pw")
    app.state.logger.removeHandler(handler)
    logged = app_log.getvalue() + "\n".join(record.getMessage() for record in caplog.records)
    assert "request_complete method=POST path=/login" in logged  # the capture works
    assert logged.count(setup_token) == 1  # only its startup line
    assert re.search(r"Generated first-run setup token: \S+", logged)
    for secret in secrets:
        assert secret not in logged, secret[:6]


@pytest.mark.parametrize(
    ("locale", "setup", "login", "security"),
    [
        ("en", "First-run setup", "Sign in", "Change password"),
        ("pl", "Konfiguracja początkowa", "Zaloguj się", "Zmień hasło"),
    ],
)
def test_auth_pages_render_in_english_and_polish(
    web: WebHarness, locale: str, setup: str, login: str, security: str
) -> None:
    app = web.app()
    headers = {"accept-language": locale}
    with TestClient(app) as client:
        assert setup in client.get("/setup", headers=headers).text
        web.complete_setup(app)
        assert login in client.get("/login", headers=headers).text
        web.sign_in(client)
        assert security in client.get("/account/password", headers=headers).text
