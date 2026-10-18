"""Invitation tokens never reach a log, with the server started the way ``serve`` starts it.

The token is in the URL path (``/invite/<token>``) and in the sign-in link's query
(``/login?next=/invite/<token>``). The app's request log redacts the path and never logs
queries; uvicorn's access log, which prints both, is off (``cli.server_options``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from assetboard.app import create_app
from assetboard.cli import server_options
from assetboard.config import Settings
from assetboard.db.models.identity import Membership, User, Workspace
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef
from assetboard.services.workspaces import create_invitation
from tests.conftest import ADMIN_PASSWORD, WebHarness, live_server

CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


def _owner_context(app: Any) -> Context:
    with app.state.session_factory() as session:
        user = session.query(User).one()
        workspace = session.query(Workspace).one()
        role = session.query(Membership.role).one()[0]
    return Context(
        user=UserRef(user.id, user.email, user.display_name, "en", True),
        workspace=WorkspaceRef(
            workspace.id, workspace.name, workspace.base_currency, "en", "UTC", None
        ),
        role=Role(role),
        clock=app.state.clock,
    )


def _walk_invitation_flow(base_url: str, token: str) -> list[httpx.Response]:
    """Every request that carries the token, as a browser would make them."""
    with httpx.Client(base_url=base_url) as client:
        page = client.get(f"/invite/{token}")
        login = client.get(f"/login?next=%2Finvite%2F{token}")
        csrf = CSRF.search(login.text)
        assert csrf is not None
        registered = client.post(
            f"/invite/{token}/register",
            data={
                "csrf_token": csrf.group(1),
                "email": "newbie@example.com",
                "display_name": "Newbie",
                "password": ADMIN_PASSWORD,
            },
        )
        reused = client.get(f"/invite/{token}")
        missing = client.get(f"/invite/{token}/nothing-here")
    return [page, login, registered, reused, missing]


@pytest.mark.parametrize("access_log", ["serve_defaults", "control_access_log_on"])
def test_invitation_token_is_absent_from_every_log(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], access_log: str
) -> None:
    web = WebHarness(tmp_path / "data")
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", secret_key="test-secret")
    app = create_app(settings)
    options = server_options(settings) | {"log_level": "info"}
    if access_log == "control_access_log_on":
        options["access_log"] = True  # proves the capture would see an access-log line
    with live_server(app, **options) as base_url:
        web.complete_setup(app)
        token = create_invitation(
            app.state.session_factory,
            _owner_context(app),
            role="viewer",
            email="newbie@example.com",
            expires_in_days=7,
        ).token
        page, login, registered, reused, missing = _walk_invitation_flow(base_url, token)
    output = "".join(capfd.readouterr())

    assert page.status_code == 200 and registered.status_code == 303
    assert reused.status_code == 404 and missing.status_code == 404
    assert login.status_code == 200
    for response in (page, reused):
        assert response.headers["referrer-policy"] == "same-origin"
        assert response.headers["cache-control"] == "no-store"
    assert "path=/invite/[redacted]" in output  # the app's own request log ran
    assert "path=/invite/[redacted]/register" in output
    if access_log == "serve_defaults":
        assert token not in output
        assert "/login?next=" not in output
    else:
        assert token in output  # uvicorn's access log prints full URLs


def test_serve_turns_the_access_log_off() -> None:
    settings = Settings(_env_file=None)
    assert server_options(settings)["access_log"] is False
