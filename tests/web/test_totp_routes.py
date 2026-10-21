from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import AuditEvent, Workspace
from assetboard.db.models.totp import TotpFactor, TotpRecoveryCode
from assetboard.db.repositories.totp import TotpRepository
from assetboard.domain.totp import recovery_hash
from assetboard.services import totp, workspaces
from assetboard.services.auth import read_web_session
from assetboard.services.context import (
    PermissionDenied,
    Role,
    TwoFactorRequired,
    build_context,
    require_role_now,
)
from assetboard.services.oidc_accounts import PendingLink, link_identity
from assetboard.web.security import session_cookie_name

if TYPE_CHECKING:
    from tests.conftest import OwnerApp


def _begin(owner: OwnerApp) -> str:
    if not isinstance(owner.app.state.clock, FixedClock):
        owner.app.state.clock = FixedClock("2026-10-21T12:00:00+00:00")
    response = owner.post("/account/totp/start", {"confirm_password": owner.web.admin_password})
    assert response.status_code == 303, response.text
    page = owner.client.get("/account/totp")
    assert "<svg" in page.text
    match = re.search(r"<code[^>]*data-totp-secret>([A-Z2-7]+)</code>", page.text)
    assert match
    return match.group(1)


def _enable(owner: OwnerApp) -> tuple[str, list[str]]:
    secret = _begin(owner)
    response = owner.post(
        "/account/totp/verify",
        {"code": pyotp.TOTP(secret).at(owner.app.state.clock.now())},
        token_from="/account/totp",
    )
    assert response.status_code == 200, response.text
    codes = re.findall(r"<li>([a-f0-9]{32})</li>", response.text)
    assert len(codes) == 10
    return secret, codes


def _login(owner: OwnerApp, client: TestClient, code: str = "", *, path: str = "/login"):
    return client.post(
        path,
        data={
            "csrf_token": owner.web.csrf(client.get(path).text),
            "email": owner.web.admin_email,
            "password": owner.web.admin_password,
            "second_factor": code,
        },
        follow_redirects=False,
    )


def _context(owner: OwnerApp):
    app = owner.app.state
    with app.session_factory() as session:
        current = read_web_session(
            session,
            owner.client.cookies[session_cookie_name(app.settings)],
            settings=app.settings,
            clock=app.clock,
        )
        assert current is not None
        return build_context(session, current, clock=app.clock)


def test_enrollment_requires_password_and_code_and_shows_recovery_codes_once(
    owner: OwnerApp,
) -> None:
    disabled = owner.post("/account/totp/disable", {"confirm_password": owner.web.admin_password})
    assert disabled.status_code == 403
    with owner.db() as session:
        assert not session.scalar(
            select(AuditEvent.id).where(AuditEvent.action == "auth.totp_disabled")
        )

    assert owner.post("/account/totp/start", {"confirm_password": "wrong"}).status_code == 401
    secret = _begin(owner)
    with owner.db() as session:
        factor = session.get(TotpFactor, owner.admin_id)
        assert factor is not None and factor.enabled_at is None
        assert secret not in factor.encrypted_secret
    assert owner.post("/account/totp/verify", {"code": "invalid"}).status_code == 400
    response = owner.post(
        "/account/totp/verify", {"code": pyotp.TOTP(secret).at(owner.app.state.clock.now())}
    )
    codes = re.findall(r"<li>([a-f0-9]{32})</li>", response.text)
    assert len(set(codes)) == 10
    with owner.db() as session:
        stored = list(session.scalars(select(TotpRecoveryCode.code_hash)))
        assert set(stored) == {recovery_hash(code) for code in codes}
        assert all(code not in stored for code in codes)
        factor = session.get(TotpFactor, owner.admin_id)
        assert factor is not None and factor.enabled_at is not None
        assert factor.enrollment_session_hash is None
    page = owner.client.get("/account/totp")
    assert page.headers["cache-control"] == "no-store"
    assert all(code not in page.text for code in codes)
    assert secret not in page.text and "<svg" not in page.text
    assert owner.post("/account/totp/verify", {"code": "123456"}).status_code == 400


def test_recovery_codes_can_be_regenerated_only_with_password_and_factor(owner: OwnerApp) -> None:
    secret, old_codes = _enable(owner)
    wrong = owner.post(
        "/account/totp/recovery-codes",
        {
            "confirm_password": owner.web.admin_password,
            "second_factor": "",
        },
    )
    assert wrong.status_code == 401
    with owner.db() as session:
        assert set(session.scalars(select(TotpRecoveryCode.code_hash))) == {
            recovery_hash(code) for code in old_codes
        }

    owner.app.state.clock = FixedClock("2026-10-21T12:00:30+00:00")
    response = owner.post(
        "/account/totp/recovery-codes",
        {
            "confirm_password": owner.web.admin_password,
            "second_factor": pyotp.TOTP(secret).at(owner.app.state.clock.now()),
        },
    )
    assert response.status_code == 200
    new_codes = re.findall(r"<li>([a-f0-9]{32})</li>", response.text)
    assert len(set(new_codes)) == 10
    assert not set(new_codes) & set(old_codes)
    with owner.db() as session:
        assert set(session.scalars(select(TotpRecoveryCode.code_hash))) == {
            recovery_hash(code) for code in new_codes
        }
        event = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "auth.totp_recovery_codes_regenerated")
        )
        assert event is not None and event.data == {}
    page = owner.client.get("/account/totp")
    assert all(code not in page.text for code in new_codes)
    assert all(code not in str(event.data) for code in new_codes)


def test_password_login_requires_factor_prevents_replay_and_consumes_recovery_once(
    owner: OwnerApp,
) -> None:
    secret, codes = _enable(owner)
    client = TestClient(owner.app)
    failed = _login(owner, client)
    assert failed.status_code == 401
    assert (
        "set-cookie" not in failed.headers
        or "assetboard_session=" not in failed.headers["set-cookie"]
    )
    enrollment_code = pyotp.TOTP(secret).at(owner.app.state.clock.now())
    assert _login(owner, client, enrollment_code).status_code == 401
    owner.app.state.clock = FixedClock("2026-10-21T12:00:30+00:00")
    code = pyotp.TOTP(secret).at(owner.app.state.clock.now())
    assert _login(owner, client, code).status_code == 303
    other = TestClient(owner.app)
    assert _login(owner, other, code).status_code == 401
    assert _login(owner, other, codes[0]).status_code == 303
    assert _login(owner, TestClient(owner.app), codes[0]).status_code == 401
    with owner.db() as session:
        assert len(list(session.scalars(select(TotpRecoveryCode)))) == 9


def test_code_failures_are_throttled_on_login_and_enrollment(owner: OwnerApp) -> None:
    _begin(owner)
    for _ in range(6):
        assert owner.post("/account/totp/verify", {"code": "invalid"}).status_code == 400
    assert (
        owner.post(
            "/account/totp/start", {"confirm_password": owner.web.admin_password}
        ).status_code
        == 303
    )
    response = owner.post("/account/totp/verify", {"code": "invalid"})
    assert response.status_code == 401 and response.headers["retry-after"] == "60"
    owner.app.state.clock = FixedClock("2026-10-21T13:00:00+00:00")
    # A password and a failed second factor share the existing sign-in limits.
    _, codes = _enable(owner)
    client = TestClient(owner.app)
    for _ in range(6):
        assert _login(owner, client, "invalid").status_code == 401
    response = _login(owner, client, codes[0])
    assert response.status_code == 401 and response.headers["retry-after"] == "60"


def test_pending_enrollment_is_session_bound_and_expires(owner: OwnerApp) -> None:
    other = TestClient(owner.app)
    assert owner.web.sign_in(other).status_code == 303
    secret = _begin(owner)
    assert secret not in other.get("/account/totp").text
    response = other.post(
        "/account/totp/verify",
        data={
            "code": pyotp.TOTP(secret).at(owner.app.state.clock.now()),
            "csrf_token": owner.web.csrf(other.get("/account/totp").text),
        },
    )
    assert response.status_code == 400
    owner.app.state.clock = FixedClock("2026-10-21T12:10:00+00:00")
    assert secret not in owner.client.get("/account/totp").text
    assert (
        owner.post(
            "/account/totp/verify", {"code": pyotp.TOTP(secret).at(owner.app.state.clock.now())}
        ).status_code
        == 400
    )


def test_enable_and_disable_revoke_other_password_sessions(owner: OwnerApp) -> None:
    other = TestClient(owner.app)
    assert owner.web.sign_in(other).status_code == 303
    _, codes = _enable(owner)
    assert other.get("/account", follow_redirects=False).status_code == 303
    assert _login(owner, other, codes[0]).status_code == 303
    assert owner.post("/account/totp/disable", {"confirm_password": "wrong"}).status_code == 401
    assert (
        owner.post(
            "/account/totp/disable", {"confirm_password": owner.web.admin_password}
        ).status_code
        == 303
    )
    assert other.get("/account", follow_redirects=False).status_code == 303
    assert owner.client.get("/").status_code == 200
    with owner.db() as session:
        assert session.get(TotpFactor, owner.admin_id) is None
        assert list(session.scalars(select(TotpRecoveryCode))) == []
        assert list(
            session.scalars(
                select(AuditEvent.action).where(
                    AuditEvent.action.in_(["auth.totp_enabled", "auth.totp_disabled"])
                )
            )
        ) == ["auth.totp_enabled", "auth.totp_disabled"]


def test_workspace_policy_is_scoped_owner_only_and_blocks_existing_local_sessions(
    owner: OwnerApp,
) -> None:
    ctx = _context(owner)
    other_workspace = owner.add_workspace("Other")
    owner.join(owner.admin_id, other_workspace, "owner")
    with pytest.raises(PermissionDenied):
        workspaces.update_settings(
            owner.app.state.session_factory,
            replace(ctx, role=Role.EDITOR),
            name="Home",
            base_currency="EUR",
            country_code=None,
            timezone="UTC",
            locale="en",
            auto_copy_plan=False,
            require_two_factor=True,
        )
    response = owner.post(
        "/workspace/settings",
        {
            "name": "Home",
            "base_currency": "EUR",
            "timezone": "UTC",
            "locale": "en",
            "require_two_factor": "1",
        },
    )
    assert response.status_code == 303
    assert owner.client.get("/", follow_redirects=False).headers["location"] == "/account/totp"
    assert "Other" in owner.client.get("/account/totp").text
    blocked = owner.client.get("/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert blocked.status_code == 204 and blocked.headers["hx-redirect"] == "/account/totp"
    with owner.db() as session:
        other = session.get(Workspace, other_workspace)
        assert other is not None and other.require_two_factor is False
        with pytest.raises(TwoFactorRequired):
            require_role_now(session, ctx, Role.OWNER)
    # Switching away leaves the unrelated workspace accessible.
    assert (
        owner.post("/workspace/switch", {"workspace_id": str(other_workspace)}).status_code == 303
    )
    assert owner.client.get("/").status_code == 200
    # The protected workspace needs verified enrollment even through a direct URL.
    owner.post("/workspace/switch", {"workspace_id": str(ctx.workspace.id)})
    _enable(owner)
    assert owner.client.get("/").status_code == 200
    owner.post("/account/totp/disable", {"confirm_password": owner.web.admin_password})
    assert owner.client.get("/", follow_redirects=False).headers["location"] == "/account/totp"


@pytest.mark.parametrize("method", ["oidc", "proxy"])
def test_external_sessions_skip_local_factor_and_workspace_requirement(
    owner: OwnerApp, method: str
) -> None:
    _enable(owner)
    with owner.app.state.session_factory.begin() as session:
        workspace = session.get(Workspace, owner.home_id)
        workspace.require_two_factor = True
        current = read_web_session(
            session,
            owner.client.cookies[session_cookie_name(owner.app.state.settings)],
            settings=owner.app.state.settings,
            clock=owner.app.state.clock,
        )
        assert current is not None
        external = replace(current, auth_method=method, second_factor_verified=False)
        assert (
            build_context(session, external, clock=owner.app.state.clock).workspace.id
            == owner.home_id
        )


def test_break_glass_local_login_cannot_skip_totp(owner: OwnerApp) -> None:
    _, codes = _enable(owner)
    settings = owner.app.state.settings
    settings.oidc_only = True
    settings.allow_local_admin_login = True
    client = TestClient(owner.app)
    assert _login(owner, client, path="/login/local").status_code == 401
    assert _login(owner, client, codes[0], path="/login/local").status_code == 303


def test_recovery_code_is_consumed_atomically_and_scoped_to_its_user(owner: OwnerApp) -> None:
    _, codes = _enable(owner)
    other_id = owner.add_user("other")
    with owner.db() as session:
        assert not TotpRepository(session, other_id).consume_recovery_code(recovery_hash(codes[0]))

    def attempt() -> bool:
        return run_immediate(
            owner.app.state.session_factory,
            lambda session: totp.verify_sign_in(
                session,
                owner.admin_id,
                code=codes[0],
                settings=owner.app.state.settings,
                now=owner.app.state.clock.now(),
            )[0],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [False, True]


def test_every_totp_post_requires_csrf(owner: OwnerApp) -> None:
    for path in ("start", "verify", "disable", "recovery-codes"):
        assert owner.client.post(f"/account/totp/{path}", data={}).status_code == 403


def test_password_confirmed_identity_linking_cannot_bypass_totp(owner: OwnerApp) -> None:
    _, codes = _enable(owner)
    settings = owner.app.state.settings
    settings.proxy_auth_enabled = True
    settings.proxy_auth_link_by_email = True
    pending = PendingLink(
        user_id=owner.admin_id,
        issuer="proxy",
        subject="new-identity",
        email=owner.web.admin_email,
        admin=None,
        next_path="/",
    )
    kwargs = {"settings": settings, "clock": owner.app.state.clock, "host": "192.0.2.10"}
    assert (
        link_identity(
            owner.app.state.session_factory, pending, owner.web.admin_password, **kwargs
        ).status
        == "failed"
    )
    assert (
        link_identity(
            owner.app.state.session_factory,
            pending,
            owner.web.admin_password,
            second_factor=codes[0],
            **kwargs,
        ).status
        == "linked"
    )


def test_recovery_still_works_if_the_instance_key_changes(owner: OwnerApp) -> None:
    secret, codes = _enable(owner)
    owner.app.state.settings.secret_key = "replacement-instance-secret"
    client = TestClient(owner.app)
    owner.app.state.clock = FixedClock("2026-10-21T12:00:30+00:00")
    assert (
        _login(owner, client, pyotp.TOTP(secret).at(owner.app.state.clock.now())).status_code == 401
    )
    assert _login(owner, client, codes[0]).status_code == 303


def test_totp_messages_are_translated_and_secrets_are_not_logged(
    owner: OwnerApp, caplog: pytest.LogCaptureFixture
) -> None:
    owner.client.headers["accept-language"] = "pl"
    page = owner.client.get("/account/totp")
    assert "Uwierzytelnianie dwuskładnikowe" in page.text
    assert "Skonfiguruj uwierzytelnianie dwuskładnikowe" in page.text
    with caplog.at_level("INFO"):
        secret, codes = _enable(owner)
    assert secret not in caplog.text
    assert all(code not in caplog.text for code in codes)


def test_instance_admin_can_reset_another_users_factor(owner: OwnerApp) -> None:
    admin_secret, _admin_recovery_codes = _enable(owner)
    target_id = owner.add_user("mfa-user")
    target_email = "mfa-user@example.com"
    target_client = TestClient(owner.app)
    assert (
        owner.web.sign_in(
            target_client, email=target_email, password=owner.web.admin_password
        ).status_code
        == 303
    )
    secret = pyotp.random_base32()
    with owner.app.state.session_factory.begin() as session:
        session.add(
            TotpFactor(
                user_id=target_id,
                encrypted_secret=totp._cipher(owner.app.state.settings)
                .encrypt(secret.encode())
                .decode(),
                enabled_at=owner.app.state.clock.now(),
                last_step=-1,
            )
        )
        session.add(TotpRecoveryCode(user_id=target_id, code_hash=recovery_hash("recovery")))

    users_page = owner.client.get("/admin/users")
    assert "Two-factor authentication enabled" in users_page.text
    assert f"/admin/users/{target_id}/reset_totp" in users_page.text
    confirm_page = owner.client.get(f"/admin/users/{target_id}/reset_totp")
    missing_factor = owner.client.post(
        f"/admin/users/{target_id}/reset_totp",
        data={
            "csrf_token": owner.web.csrf(confirm_page.text),
            "confirm_password": owner.web.admin_password,
        },
        follow_redirects=False,
    )
    assert missing_factor.status_code == 401
    with owner.db() as session:
        assert session.get(TotpFactor, target_id) is not None

    owner.app.state.clock = FixedClock("2026-10-21T12:00:30+00:00")
    confirm_page = owner.client.get(f"/admin/users/{target_id}/reset_totp")
    response = owner.client.post(
        f"/admin/users/{target_id}/reset_totp",
        data={
            "csrf_token": owner.web.csrf(confirm_page.text),
            "confirm_password": owner.web.admin_password,
            "second_factor": pyotp.TOTP(admin_secret).at(owner.app.state.clock.now()),
        },
        follow_redirects=False,
    )
    assert response.status_code == 200 and "data-admin-notice" in response.text
    assert target_client.get("/account", follow_redirects=False).status_code == 303
    with owner.db() as session:
        assert session.get(TotpFactor, target_id) is None
        assert (
            list(
                session.scalars(
                    select(TotpRecoveryCode).where(TotpRecoveryCode.user_id == target_id)
                )
            )
            == []
        )
        event = session.scalar(select(AuditEvent).where(AuditEvent.action == "admin.totp_reset"))
        assert event is not None
        assert event.entity_id == str(target_id)
        assert event.actor_user_id == owner.admin_id
        assert event.data == {"sessions_revoked": 1}
