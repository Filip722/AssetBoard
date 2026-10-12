from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from assetboard.db.models.identity import AuditEvent
from assetboard.db.repositories import NotFound
from assetboard.services import workspaces
from assetboard.services.context import Context, PermissionDenied, Role
from assetboard.services.workspaces import (
    InvalidWorkspaceSettings,
    LastOwnerError,
    SelfRemovalError,
    change_role,
    create_invitation,
    list_invitations,
    list_members,
    remove_member,
    revoke_invitation,
    update_settings,
    workspace_settings,
)
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

SETTINGS = {
    "name": "Family",
    "base_currency": "PLN",
    "country_code": "PL",
    "timezone": "Europe/Warsaw",
    "locale": "pl",
}


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


@pytest.fixture
def team(db: WorkspaceDb) -> WorkspaceDb:
    db.add_member("olga", "owner")
    db.add_member("ed", "editor")
    db.add_member("vic", "viewer")
    return db


def _events(db: WorkspaceDb) -> list[tuple[str, dict[str, Any]]]:
    with db.factory() as session:
        rows = session.scalars(select(AuditEvent).order_by(AuditEvent.ts, AuditEvent.action))
        return [(event.action, event.data) for event in rows]


# Owner matrix: every public function of services/workspaces.py, found by introspection.

ServiceCall = Callable[[WorkspaceDb, Context], object]
SERVICE_CALLS: dict[str, ServiceCall] = {
    "workspace_settings": lambda db, ctx: workspace_settings(db.factory, ctx),
    "update_settings": lambda db, ctx: update_settings(db.factory, ctx, **SETTINGS),
    "list_members": lambda db, ctx: list_members(db.factory, ctx),
    "change_role": lambda db, ctx: change_role(
        db.factory, ctx, membership_id=db.memberships["vic"], role=Role.EDITOR
    ),
    "remove_member": lambda db, ctx: remove_member(
        db.factory, ctx, membership_id=db.memberships["vic"]
    ),
    "list_invitations": lambda db, ctx: list_invitations(db.factory, ctx),
    "create_invitation": lambda db, ctx: create_invitation(
        db.factory, ctx, role=Role.EDITOR, email=None, expires_in_days=7
    ),
    "revoke_invitation": lambda db, ctx: revoke_invitation(db.factory, ctx, invitation_id=uuid4()),
}


def _public_functions() -> set[str]:
    return {
        name
        for name, value in vars(workspaces).items()
        if inspect.isfunction(value)
        and value.__module__ == workspaces.__name__
        and not name.startswith("_")
    }


def test_owner_matrix_registry_matches_the_service_module() -> None:
    assert set(SERVICE_CALLS) == _public_functions(), "add or remove SERVICE_CALLS entries"


@pytest.mark.parametrize("name", sorted(SERVICE_CALLS))
@pytest.mark.parametrize("member", ["ed", "vic"])
def test_service_functions_require_owner(team: WorkspaceDb, name: str, member: str) -> None:
    role = "editor" if member == "ed" else "viewer"
    roles_before = team.roles()
    with pytest.raises(PermissionDenied):
        SERVICE_CALLS[name](team, team.ctx(member, role))
    assert team.roles() == roles_before
    assert _events(team) == []


@pytest.mark.parametrize("change", ["demoted", "removed"])
@pytest.mark.parametrize("name", sorted(SERVICE_CALLS))
def test_service_writes_recheck_the_role_in_the_transaction(
    team: WorkspaceDb, name: str, change: str
) -> None:
    # Olga's Context still says "owner", but meanwhile Ed demoted or removed her.
    olga, ed = team.ctx("olga", "owner"), team.ctx("ed", "owner")
    change_role(team.factory, olga, membership_id=team.memberships["ed"], role=Role.OWNER)
    if change == "demoted":
        change_role(team.factory, ed, membership_id=team.memberships["olga"], role=Role.EDITOR)
    else:
        remove_member(team.factory, ed, membership_id=team.memberships["olga"])
    roles_before, events_before = team.roles(), _events(team)
    if name in {"workspace_settings", "list_members", "list_invitations"}:
        SERVICE_CALLS[name](team, olga)  # reads use the request's Context
        return
    with pytest.raises(PermissionDenied):
        SERVICE_CALLS[name](team, olga)
    assert team.roles() == roles_before
    assert _events(team) == events_before


# Settings


def test_update_settings_changes_the_workspace_and_audits_field_names(team: WorkspaceDb) -> None:
    owner = team.ctx("olga", "owner")
    update_settings(team.factory, owner, **SETTINGS)
    assert workspace_settings(team.factory, owner) == workspaces.WorkspaceSettings(
        name="Family",
        base_currency="PLN",
        country_code="PL",
        timezone="Europe/Warsaw",
        locale="pl",
    )
    assert _events(team) == [
        (
            "workspace.settings_updated",
            {"fields": "base_currency,country_code,locale,name,timezone"},
        )
    ]
    update_settings(team.factory, owner, **SETTINGS)  # unchanged: no second event
    assert len(_events(team)) == 1


@pytest.mark.parametrize(
    ("changes", "field", "code"),
    [
        ({"name": "   "}, "name", "invalid_workspace_name"),
        ({"name": "x" * 121}, "name", "invalid_workspace_name"),
        ({"base_currency": "XYZ"}, "base_currency", "invalid_base_currency"),
        ({"timezone": "Mars/Base"}, "timezone", "invalid_timezone"),
        ({"country_code": "ZZ"}, "country_code", "invalid_country_code"),
        ({"locale": "xx"}, "locale", "invalid_locale"),
    ],
)
def test_update_settings_validates_input(
    team: WorkspaceDb, changes: dict[str, str], field: str, code: str
) -> None:
    with pytest.raises(InvalidWorkspaceSettings) as invalid:
        update_settings(team.factory, team.ctx("olga", "owner"), **(SETTINGS | changes))
    assert (invalid.value.field, invalid.value.code) == (field, code)
    assert _events(team) == []


# Roles and the last-owner rule


def test_owner_changes_roles_and_it_is_audited(team: WorkspaceDb) -> None:
    change_role(
        team.factory,
        team.ctx("olga", "owner"),
        membership_id=team.memberships["vic"],
        role="editor",
    )
    assert team.roles()["vic"] == "editor"
    assert _events(team) == [
        (
            "member.role_changed",
            {"user_id": str(team.users["vic"]), "from": "viewer", "to": "editor"},
        )
    ]


def test_last_owner_cannot_demote_themselves(team: WorkspaceDb) -> None:
    with pytest.raises(LastOwnerError):
        change_role(
            team.factory,
            team.ctx("olga", "owner"),
            membership_id=team.memberships["olga"],
            role=Role.EDITOR,
        )
    assert team.roles()["olga"] == "owner"


def test_self_demotion_is_allowed_when_another_owner_exists(team: WorkspaceDb) -> None:
    owner = team.ctx("olga", "owner")
    change_role(team.factory, owner, membership_id=team.memberships["ed"], role=Role.OWNER)
    change_role(team.factory, owner, membership_id=team.memberships["olga"], role=Role.VIEWER)
    assert team.roles() == {"olga": "viewer", "ed": "owner", "vic": "viewer"}


def test_owner_can_demote_another_owner_but_not_the_last_one(team: WorkspaceDb) -> None:
    olga = team.ctx("olga", "owner")
    change_role(team.factory, olga, membership_id=team.memberships["ed"], role=Role.OWNER)
    change_role(team.factory, olga, membership_id=team.memberships["ed"], role=Role.VIEWER)
    assert team.roles()["ed"] == "viewer"
    with pytest.raises(LastOwnerError):
        change_role(team.factory, olga, membership_id=team.memberships["olga"], role="viewer")


def test_setting_the_same_role_is_a_no_op(team: WorkspaceDb) -> None:
    change_role(
        team.factory, team.ctx("olga", "owner"), membership_id=team.memberships["ed"], role="editor"
    )
    assert _events(team) == []


def test_unknown_role_is_rejected_before_any_write(team: WorkspaceDb) -> None:
    with pytest.raises(ValueError):
        change_role(
            team.factory, team.ctx("olga", "owner"), membership_id=team.memberships["ed"], role="x"
        )


# Removal


def test_owner_removes_a_member_and_it_is_audited(team: WorkspaceDb) -> None:
    remove_member(team.factory, team.ctx("olga", "owner"), membership_id=team.memberships["vic"])
    assert "vic" not in team.roles()
    assert _events(team) == [
        ("member.removed", {"user_id": str(team.users["vic"]), "role": "viewer"})
    ]


def test_owners_cannot_remove_themselves(team: WorkspaceDb) -> None:
    owner = team.ctx("olga", "owner")
    change_role(team.factory, owner, membership_id=team.memberships["ed"], role=Role.OWNER)
    with pytest.raises(SelfRemovalError):
        remove_member(team.factory, owner, membership_id=team.memberships["olga"])
    assert team.roles()["olga"] == "owner"


def test_memberships_of_another_workspace_are_not_found(team: WorkspaceDb) -> None:
    other = team.add_workspace("Other")
    foreign = team.add_member("stranger", "viewer", workspace_id=other)
    owner = team.ctx("olga", "owner")
    for call in (
        lambda: change_role(team.factory, owner, membership_id=foreign, role=Role.OWNER),
        lambda: remove_member(team.factory, owner, membership_id=foreign),
        lambda: change_role(team.factory, owner, membership_id=uuid4(), role=Role.OWNER),
    ):
        with pytest.raises(NotFound):
            call()
    assert team.roles(other) == {"stranger": "viewer"}


def test_list_members_returns_this_workspace_only(team: WorkspaceDb) -> None:
    other = team.add_workspace("Other")
    team.add_member("stranger", "owner", workspace_id=other)
    members = list_members(team.factory, team.ctx("olga", "owner"))
    assert {(m.display_name, m.role) for m in members} == {
        ("Olga", "owner"),
        ("Ed", "editor"),
        ("Vic", "viewer"),
    }
