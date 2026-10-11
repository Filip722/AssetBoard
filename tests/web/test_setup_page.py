from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from assetboard.db.models.identity import User, Workspace
from assetboard.services.auth import SetupAlreadyCompleted

if TYPE_CHECKING:
    from conftest import CountingHasher, WebHarness

SETUP_RACE = "Setup was already completed, or is being completed. Reload to continue."


def _form(
    client: TestClient, web: WebHarness, token: str | None, **overrides: str
) -> dict[str, str]:
    values = {
        "csrf_token": web.csrf(client.get("/setup").text),
        "setup_token": token or "",
        "email": "Owner@Example.com",
        "display_name": "Owner",
        "password": web.admin_password,
        "workspace_name": "Household",
        "base_currency": "PLN",
        "country_code": "PL",
        "timezone": "Europe/Warsaw",
        "workspace_locale": "pl",
    }
    values.update(overrides)
    return values


def test_setup_form_has_token_field_and_option_lists(web: WebHarness) -> None:
    with TestClient(web.app()) as client:
        page = client.get("/setup", headers={"accept-language": "pl"})
    assert page.status_code == 200
    assert 'name="setup_token"' in page.text and 'autocomplete="off"' in page.text
    assert '<option value="PLN"' in page.text
    assert '<option value="Europe/Warsaw"' in page.text
    assert ">Polska</option>" in page.text
    assert 'autocomplete="new-password"' in page.text


def test_setup_creates_admin_signs_in_and_rotates_the_csrf_cookie(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app) as client:
        response = client.post(
            "/setup", data=_form(client, web, app.state.setup_token), follow_redirects=False
        )
        assert (response.status_code, response.headers["location"]) == (303, "/")
        cookies = response.headers.get_list("set-cookie")
        assert any(c.startswith("assetboard_session=") and "Max-Age=" in c for c in cookies)
        assert any(c.startswith("assetboard_csrf=") and "Max-Age=0" in c for c in cookies)
        home = client.get("/")
        assert home.status_code == 200 and "Owner" in home.text
        assert client.get("/setup").status_code == 404
        assert app.state.setup_token is None
    with app.state.session_factory() as session:
        user = session.scalars(select(User)).one()
        workspace = session.scalars(select(Workspace)).one()
    assert (user.email, user.is_instance_admin) == ("owner@example.com", True)
    assert (workspace.base_currency, workspace.timezone, workspace.locale) == (
        "PLN",
        "Europe/Warsaw",
        "pl",
    )


@pytest.mark.parametrize("token", ["", "wrong", "x" * 43, "zażółć gęślą jaźń"])
def test_wrong_setup_token_is_refused(web: WebHarness, token: str) -> None:
    app = web.app()
    with TestClient(app) as client:
        response = client.post("/setup", data=_form(client, web, None, setup_token=token))
        assert response.status_code == 400
        assert "The setup token is not correct." in response.text
        assert client.get("/setup").status_code == 200  # still open


def test_setup_token_attempts_are_throttled(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app, client=("192.0.2.50", 1000)) as client:
        for _ in range(app.state.settings.setup_throttle_free_failures + 1):
            client.post("/setup", data=_form(client, web, None, setup_token="wrong"))
        blocked = client.post("/setup", data=_form(client, web, app.state.setup_token))
        assert blocked.status_code == 400  # even the right token, while backing off
        assert client.get("/setup").status_code == 200


def test_setup_validation_errors_are_shown_on_the_field(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app) as client:
        response = client.post(
            "/setup", data=_form(client, web, app.state.setup_token, password="qwertyuiop")
        )
    assert response.status_code == 400
    assert "This password is too common." in response.text
    assert 'aria-invalid="true" aria-describedby="password-error"' in response.text


def test_setup_loser_gets_409(web: WebHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    app = web.app()

    async def lost(*_args: object, **_kwargs: object) -> object:
        raise SetupAlreadyCompleted

    monkeypatch.setattr("assetboard.web.auth_routes.run_initial_setup_async", lost)
    with TestClient(app) as client:
        response = client.post("/setup", data=_form(client, web, app.state.setup_token))
        htmx = client.post(
            "/setup",
            data=_form(client, web, app.state.setup_token),
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 409 and SETUP_RACE in response.text
    assert htmx.status_code == 409 and htmx.headers["hx-retarget"] == "#toast-container"


@pytest.mark.parametrize(
    "field",
    ["password", "email", "display_name", "workspace_name", "setup_token"],
)
def test_oversized_setup_fields_are_refused_before_hashing(
    web: WebHarness, argon2_calls: CountingHasher, field: str
) -> None:
    app = web.app()
    oversized = {"password": "p" * 1025, "email": "e" * 321, "setup_token": "t" * 257}.get(
        field, "n" * 121
    )
    with TestClient(app) as client:
        before = (argon2_calls.hashes, argon2_calls.verifies)
        response = client.post(
            "/setup", data=_form(client, web, app.state.setup_token, **{field: oversized})
        )
        assert response.status_code == 400
        assert "This value is too long." in response.text
        assert (argon2_calls.hashes, argon2_calls.verifies) == before
        assert client.get("/setup").status_code == 200  # setup stays open
