from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from assetboard.clock import FixedClock
from assetboard.db.models.identity import Membership, User, UserSession, Workspace
from assetboard.services.auth import WebSession
from assetboard.services.context import (
    ROLE_RANK,
    NoWorkspace,
    PermissionDenied,
    Role,
    WorkspaceNotAvailable,
    build_context,
    require_role,
    require_role_now,
    select_workspace,
)
from tests.tenant.harness import context_for

ALLOWED = {
    (Role.OWNER, Role.OWNER): True,
    (Role.OWNER, Role.EDITOR): True,
    (Role.OWNER, Role.VIEWER): True,
    (Role.EDITOR, Role.OWNER): False,
    (Role.EDITOR, Role.EDITOR): True,
    (Role.EDITOR, Role.VIEWER): True,
    (Role.VIEWER, Role.OWNER): False,
    (Role.VIEWER, Role.EDITOR): False,
    (Role.VIEWER, Role.VIEWER): True,
}


def test_role_ranks_are_explicit() -> None:
    assert ROLE_RANK == {Role.VIEWER: 1, Role.EDITOR: 2, Role.OWNER: 3}
    assert set(ROLE_RANK) == set(Role)


@pytest.mark.parametrize(("actual", "required"), sorted(ALLOWED))
def test_require_role_matrix(fixed_clock: FixedClock, actual: Role, required: Role) -> None:
    ctx = context_for(uuid4(), fixed_clock, role=actual)
    if ALLOWED[(actual, required)]:
        require_role(ctx, required)
        require_role(ctx, required.value)  # the string form
    else:
        with pytest.raises(PermissionDenied) as denied:
            require_role(ctx, required)
        assert (denied.value.required, denied.value.actual) == (required, actual)


def test_require_role_rejects_unknown_roles(fixed_clock: FixedClock) -> None:
    with pytest.raises(ValueError):
        require_role(context_for(uuid4(), fixed_clock), "admin")


# build_context and select_workspace


def _user(session: Session, email: str) -> User:
    user = User(email=email, display_name=email.split("@")[0])
    session.add(user)
    session.flush()
    return user


def _workspace(session: Session, name: str, currency: str = "EUR") -> Workspace:
    workspace = Workspace(name=name, base_currency=currency, locale="pl", timezone="Europe/Warsaw")
    session.add(workspace)
    session.flush()
    return workspace


def _join(
    session: Session, workspace: Workspace, user: User, role: str, clock: FixedClock, minute: int
) -> None:
    # Explicit created_at values make "oldest membership" observable.
    joined = clock.now() + timedelta(minutes=minute)
    session.add(
        Membership(workspace_id=workspace.id, user_id=user.id, role=role, created_at=joined)
    )
    session.flush()


def _web_session(session: Session, user: User, clock: FixedClock) -> tuple[WebSession, str]:
    session_hash = uuid4().hex * 2
    now = clock.now()
    session.add(
        UserSession(
            id_hash=session_hash,
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(days=1),
        )
    )
    session.flush()
    return _as_web_session(user, session_hash, None, clock), session_hash


def _as_web_session(
    user: User, session_hash: str, selected: UUID | None, clock: FixedClock
) -> WebSession:
    return WebSession(
        session_hash=session_hash,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        locale=user.locale or "en",
        is_instance_admin=False,
        expires_at=clock.now() + timedelta(days=1),
        needs_touch=False,
        current_workspace_id=selected,
    )


def test_build_context_uses_the_selected_workspace(
    db_session: Session, fixed_clock: FixedClock
) -> None:
    user = _user(db_session, "pat@example.com")
    first, second = _workspace(db_session, "First"), _workspace(db_session, "Second", "PLN")
    _join(db_session, first, user, "owner", fixed_clock, 1)
    _join(db_session, second, user, "viewer", fixed_clock, 2)
    web, session_hash = _web_session(db_session, user, fixed_clock)

    selected = _as_web_session(user, session_hash, second.id, fixed_clock)
    ctx = build_context(db_session, selected, clock=fixed_clock)
    assert (ctx.workspace.id, ctx.workspace.name, ctx.workspace.base_currency) == (
        second.id,
        "Second",
        "PLN",
    )
    assert ctx.role is Role.VIEWER
    assert ctx.user.id == user.id and ctx.clock is fixed_clock
    assert build_context(db_session, web, clock=fixed_clock).workspace.id == first.id


def test_build_context_falls_back_to_the_oldest_membership_without_storing_it(
    db_session: Session, fixed_clock: FixedClock
) -> None:
    user = _user(db_session, "sam@example.com")
    older, newer, foreign = (_workspace(db_session, name) for name in ("Older", "Newer", "Foreign"))
    _join(db_session, newer, user, "editor", fixed_clock, 5)  # inserted first, joined later
    _join(db_session, older, user, "owner", fixed_clock, 1)
    _web, session_hash = _web_session(db_session, user, fixed_clock)

    for selected in (None, foreign.id, uuid4()):  # nothing, not a member, does not exist
        web = _as_web_session(user, session_hash, selected, fixed_clock)
        ctx = build_context(db_session, web, clock=fixed_clock)
        assert (ctx.workspace.id, ctx.role) == (older.id, Role.OWNER)
    row = db_session.get(UserSession, session_hash)
    assert row is not None and row.current_workspace_id is None


def test_build_context_without_memberships_raises_no_workspace(
    db_session: Session, fixed_clock: FixedClock
) -> None:
    user = _user(db_session, "lee@example.com")
    web, _hash = _web_session(db_session, user, fixed_clock)
    with pytest.raises(NoWorkspace):
        build_context(db_session, web, clock=fixed_clock)


def test_select_workspace_stores_a_membership_and_refuses_others(
    db_session: Session, fixed_clock: FixedClock
) -> None:
    user, stranger = _user(db_session, "kim@example.com"), _user(db_session, "x@example.com")
    mine, foreign = _workspace(db_session, "Mine"), _workspace(db_session, "Foreign")
    _join(db_session, mine, user, "editor", fixed_clock, 1)
    _join(db_session, foreign, stranger, "owner", fixed_clock, 1)
    _web, session_hash = _web_session(db_session, user, fixed_clock)
    _other, stranger_hash = _web_session(db_session, stranger, fixed_clock)

    select_workspace(db_session, session_hash=session_hash, user_id=user.id, workspace_id=mine.id)
    row = db_session.get(UserSession, session_hash)
    assert row is not None and row.current_workspace_id == mine.id

    for workspace_id in (foreign.id, uuid4()):
        with pytest.raises(WorkspaceNotAvailable):
            select_workspace(
                db_session, session_hash=session_hash, user_id=user.id, workspace_id=workspace_id
            )
    # Someone else's session cannot be pointed at a workspace, even one the caller belongs to.
    with pytest.raises(WorkspaceNotAvailable):
        select_workspace(
            db_session, session_hash=stranger_hash, user_id=user.id, workspace_id=mine.id
        )
    assert row.current_workspace_id == mine.id
    stranger_row = db_session.get(UserSession, stranger_hash)
    assert stranger_row is not None and stranger_row.current_workspace_id is None


def test_fallback_breaks_created_at_ties_by_membership_id(
    db_session: Session, fixed_clock: FixedClock
) -> None:
    user = _user(db_session, "tie@example.com")
    workspaces = [_workspace(db_session, f"Tie {index}") for index in range(3)]
    for workspace in workspaces:
        _join(db_session, workspace, user, "viewer", fixed_clock, 1)
    web, _hash = _web_session(db_session, user, fixed_clock)
    memberships = db_session.query(Membership).filter(Membership.user_id == user.id).all()
    lowest = min(memberships, key=lambda membership: membership.id)
    assert build_context(db_session, web, clock=fixed_clock).workspace.id == lowest.workspace_id


# require_role_now (F2.1): the role as stored now, not as the Context remembers it


@pytest.mark.parametrize(("stored", "required"), sorted(ALLOWED))
def test_require_role_now_reads_the_stored_role(
    db_session: Session, fixed_clock: FixedClock, stored: Role, required: Role
) -> None:
    workspace = _workspace(db_session, "Home")
    user = _user(db_session, "member@example.com")
    _join(db_session, workspace, user, stored.value, fixed_clock, 0)
    # The Context claims owner; only the stored membership counts.
    ctx = context_for(workspace.id, fixed_clock, role=Role.OWNER)
    ctx = replace(ctx, user=replace(ctx.user, id=user.id))
    if ALLOWED[(stored, required)]:
        require_role_now(db_session, ctx, required)
        require_role_now(db_session, ctx, required.value)
    else:
        with pytest.raises(PermissionDenied) as denied:
            require_role_now(db_session, ctx, required)
        assert (denied.value.required, denied.value.actual) == (required, stored)


def test_require_role_now_refuses_a_removed_member(
    db_session: Session, fixed_clock: FixedClock
) -> None:
    workspace = _workspace(db_session, "Home")
    other = _workspace(db_session, "Other")
    user = _user(db_session, "gone@example.com")
    _join(db_session, other, user, "owner", fixed_clock, 0)  # an owner elsewhere only
    ctx = context_for(workspace.id, fixed_clock, role=Role.OWNER)
    ctx = replace(ctx, user=replace(ctx.user, id=user.id))
    for required in Role:
        with pytest.raises(PermissionDenied) as denied:
            require_role_now(db_session, ctx, required)
        assert denied.value.actual is Role.VIEWER
