"""Instance administration services (F1.5 part B)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update

from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import AuditEvent, SetupState, User
from assetboard.db.models.totp import TotpFactor, TotpRecoveryCode
from assetboard.domain.totp import recovery_hash
from assetboard.services import admin_users
from assetboard.services.admin_users import (
    AUDIT_DATA_KEYS,
    NotInstanceAdmin,
    apply_admin_action,
    generate_temporary_password,
)
from assetboard.services.auth import PasswordPolicyError, validate_password
from tests.tenant.serialize import serialize_at_checkpoint
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for database in workspace_db(tmp_path):
        with database.factory.begin() as session:
            session.execute(update(SetupState).values(completed_at=database.clock.now()))
        yield database


def add_user(db: WorkspaceDb, email: str, *, admin: bool = False, active: bool = True) -> UUID:
    with db.factory.begin() as session:
        user = User(
            email=email, display_name=email.split("@")[0], is_instance_admin=admin, is_active=active
        )
        session.add(user)
        session.flush()
        return user.id


def run(db: WorkspaceDb, actor_id: UUID, target_id: UUID, action: Any) -> str:
    def apply(session: Any) -> str:
        actor = session.get(User, actor_id)
        return apply_admin_action(
            session, actor, target_id, action, clock=db.clock, host=None, user_agent=None
        )

    return run_immediate(db.factory, apply)


def admins(db: WorkspaceDb) -> list[str]:
    with db.factory() as session:
        return sorted(
            session.scalars(
                select(User.email).where(User.is_instance_admin.is_(True), User.is_active.is_(True))
            ).all()
        )


def test_two_administrators_demoting_each_other_at_once_keep_one(
    db: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = add_user(db, "a@example.com", admin=True)
    b = add_user(db, "b@example.com", admin=True)
    results: dict[str, str] = {}

    def demote(name: str, actor: UUID, target: UUID) -> None:
        results[name] = run(db, actor, target, "demote")

    outcomes = serialize_at_checkpoint(
        db.engine,
        monkeypatch,
        admin_users,
        lambda: demote("first", a, b),
        lambda: demote("second", b, a),
    )
    # The first demotes b; b is no longer an administrator when its own action runs.
    assert outcomes["first"] is None and results["first"] == "done"
    assert isinstance(outcomes["second"], NotInstanceAdmin)
    assert admins(db) == ["a@example.com"]


def test_two_administrators_deactivating_each_other_at_once_keep_one(
    db: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = add_user(db, "a@example.com", admin=True)
    b = add_user(db, "b@example.com", admin=True)
    outcomes = serialize_at_checkpoint(
        db.engine,
        monkeypatch,
        admin_users,
        lambda: run(db, a, b, "deactivate"),
        lambda: run(db, b, a, "deactivate"),
    )
    assert outcomes["first"] is None and isinstance(outcomes["second"], NotInstanceAdmin)
    assert admins(db) == ["a@example.com"]


def test_the_last_active_administrator_is_kept(db: WorkspaceDb) -> None:
    actor = add_user(db, "a@example.com", admin=True)
    # Another administrator that is deactivated does not count as a remaining one.
    other = add_user(db, "b@example.com", admin=True, active=False)
    assert run(db, actor, actor, "demote") == "self"
    assert run(db, actor, actor, "deactivate") == "self"
    assert run(db, actor, other, "demote") == "done"  # demoting an inactive admin is fine
    assert admins(db) == ["a@example.com"]


def test_a_non_administrator_cannot_act(db: WorkspaceDb) -> None:
    actor = add_user(db, "user@example.com")
    target = add_user(db, "other@example.com")
    for action in ("activate", "deactivate", "promote", "demote", "reset_totp"):
        with pytest.raises(NotInstanceAdmin):
            run(db, actor, target, action)


def test_audit_data_keys_are_named_for_every_action() -> None:
    assert AUDIT_DATA_KEYS == {
        "admin.user_created": ("temporary_password",),
        "admin.user_activated": (),
        "admin.user_deactivated": ("sessions_revoked",),
        "admin.admin_granted": (),
        "admin.admin_revoked": (),
        "admin.totp_reset": ("sessions_revoked",),
    }


def test_admin_totp_reset_clears_factor_codes_and_rejects_self_reset(db: WorkspaceDb) -> None:
    actor_id = add_user(db, "admin@example.com", admin=True)
    target_id = add_user(db, "target@example.com")
    with db.factory.begin() as session:
        session.add(
            TotpFactor(
                user_id=target_id,
                encrypted_secret="encrypted",
                enabled_at=db.clock.now(),
                last_step=42,
            )
        )
        session.add(TotpRecoveryCode(user_id=target_id, code_hash=recovery_hash("recovery")))

    assert run(db, actor_id, actor_id, "reset_totp") == "self_totp_reset"
    assert run(db, actor_id, target_id, "reset_totp") == "done"
    assert run(db, actor_id, target_id, "reset_totp") == "unchanged"
    with db.factory() as session:
        assert session.get(TotpFactor, target_id) is None
        assert (
            list(
                session.scalars(
                    select(TotpRecoveryCode).where(TotpRecoveryCode.user_id == target_id)
                )
            )
            == []
        )
        audit = session.scalar(select(AuditEvent).where(AuditEvent.action == "admin.totp_reset"))
        assert audit is not None and audit.data == {"sessions_revoked": 0}


def test_temporary_passwords_pass_the_policy_and_differ() -> None:
    passwords = {generate_temporary_password("new@example.com", "New") for _ in range(50)}
    assert len(passwords) == 50
    for password in passwords:
        assert len(password) >= 16
        validate_password(password, "new@example.com", "New")


def test_temporary_password_generation_gives_up_after_twenty_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    def always_refuse(password: str, _email: str, _name: str) -> None:
        attempts.append(password)
        raise PasswordPolicyError("password_too_common", "refused")

    monkeypatch.setattr(admin_users, "validate_password", always_refuse)
    with pytest.raises(RuntimeError, match="temporary password"):
        generate_temporary_password("new@example.com", "New")
    assert len(attempts) == 20


@pytest.mark.parametrize("action", ["deactivate", "demote"])
def test_the_last_administrator_guard_on_its_own(db: WorkspaceDb, action: Any) -> None:
    # The guard behind apply_admin_action's actor check: an actor that is no longer an active
    # administrator (for example demoted meanwhile) must still not remove the last one.
    former = add_user(db, "former@example.com", admin=False)
    last = add_user(db, "last@example.com", admin=True)
    with db.factory() as session:
        actor, target = session.get(User, former), session.get(User, last)
        assert actor is not None and target is not None
        assert admin_users._protection(session, actor, target, action) == "last_admin"
        assert admin_users._protection(session, target, target, action) == "self"
        assert admin_users._protection(session, actor, target, "promote") is None
