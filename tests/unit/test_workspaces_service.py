from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import ORMExecuteState, Session
from sqlalchemy.sql import ClauseElement

from assetboard.db.models.identity import AuditEvent
from assetboard.db.models.ledger import Category, Transaction
from assetboard.db.repositories import NotFound
from assetboard.domain.months import YearMonth
from assetboard.services import workspaces
from assetboard.services.budget import set_planned, workspace_has_budget_lines
from assetboard.services.categories import create_category
from assetboard.services.context import Context, PermissionDenied, Role
from assetboard.services.transactions import (
    create_transaction,
    delete_transaction,
    workspace_has_transactions,
)
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

SETTINGS: dict[str, Any] = {
    "name": "Family",
    "base_currency": "PLN",
    "country_code": "PL",
    "timezone": "Europe/Warsaw",
    "locale": "pl",
    "auto_copy_plan": False,
}


def _settings(**changes: Any) -> dict[str, Any]:
    return SETTINGS | changes


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
        db.factory, ctx, role=Role.EDITOR, email="x@example.com", expires_in_days=7
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


# Public functions that are not for owners, each with where its access rule is tested.
NOT_OWNER_ONLY: dict[str, str] = {
    "create_own_workspace": (
        "any signed-in account while the instance setting allows it; "
        "tests/unit/test_workspace_creation_service.py"
    ),
}


def test_owner_matrix_registry_matches_the_service_module() -> None:
    assert set(SERVICE_CALLS) | set(NOT_OWNER_ONLY) == _public_functions(), (
        "add or remove SERVICE_CALLS entries"
    )
    assert not set(SERVICE_CALLS) & set(NOT_OWNER_ONLY)


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


def test_auto_copy_plan_is_off_by_default_and_owners_turn_it_on(team: WorkspaceDb) -> None:
    owner = team.ctx("olga", "owner")
    assert workspace_settings(team.factory, owner).auto_copy_plan is False
    saved = update_settings(team.factory, owner, **_settings(auto_copy_plan=True))
    assert saved.auto_copy_plan is True
    assert workspace_settings(team.factory, owner).auto_copy_plan is True
    assert _events(team)[-1] == (
        "workspace.settings_updated",
        {"fields": "auto_copy_plan,base_currency,country_code,locale,name,timezone"},
    )
    update_settings(team.factory, owner, **SETTINGS)
    assert workspace_settings(team.factory, owner).auto_copy_plan is False
    assert _events(team)[-1] == ("workspace.settings_updated", {"fields": "auto_copy_plan"})


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
        update_settings(team.factory, team.ctx("olga", "owner"), **_settings(**changes))
    assert (invalid.value.field, invalid.value.code) == (field, code)
    assert _events(team) == []


# The base currency is locked once amounts exist (F2.2, owner decision)


def _spend(db: WorkspaceDb) -> None:
    editor = db.ctx("ed", "editor")
    food = create_category(
        db.factory, editor, name="Food", kind="expense", treatment="regular", color="#16a34a"
    )
    create_transaction(
        db.factory, editor, month=YearMonth(2026, 3), direction="expense", day="7",
        amount="12.50", description="", category_id=food.id, locale="en",
    )  # fmt: skip


def _currency(db: WorkspaceDb) -> str:
    return workspace_settings(db.factory, db.ctx("olga", "owner")).base_currency


def test_the_base_currency_is_free_while_no_amounts_exist(team: WorkspaceDb) -> None:
    owner = team.ctx("olga", "owner")
    assert workspace_settings(team.factory, owner).base_currency_locked is False
    saved = update_settings(team.factory, owner, **SETTINGS)
    assert (saved.base_currency, saved.base_currency_locked) == ("PLN", False)


@pytest.mark.parametrize("deleted", [False, True])
def test_the_base_currency_is_locked_once_a_transaction_exists(
    team: WorkspaceDb, deleted: bool
) -> None:
    _spend(team)
    if deleted:  # a soft-deleted transaction still holds an amount in the currency
        with team.factory() as session:
            row_id = session.scalars(select(Transaction.id)).one()
        delete_transaction(team.factory, team.ctx("ed", "editor"), row_id)
    owner = team.ctx("olga", "owner")
    assert workspace_settings(team.factory, owner).base_currency_locked is True
    events_before = _events(team)
    with pytest.raises(workspaces.BaseCurrencyLocked) as locked:
        update_settings(team.factory, owner, **SETTINGS)
    assert (locked.value.field, locked.value.code) == ("base_currency", "base_currency_locked")
    assert _currency(team) == "EUR" and _events(team) == events_before
    # Every other setting still changes.
    saved = update_settings(team.factory, owner, **_settings(base_currency="EUR"))
    assert (saved.name, saved.base_currency, saved.base_currency_locked) == ("Family", "EUR", True)


def _plan(db: WorkspaceDb, amount: str = "550") -> None:
    editor = db.ctx("ed", "editor")
    food = create_category(
        db.factory, editor, name="Food", kind="expense", treatment="regular", color="#16a34a"
    )
    set_planned(
        db.factory, editor, month=YearMonth(2026, 3), category_id=food.id, amount=amount,
        locale="en",
    )  # fmt: skip


def test_a_planned_value_locks_the_base_currency_while_it_exists(team: WorkspaceDb) -> None:
    """F2.3 (owner decision P4): lines are removed, not soft-deleted, so only existing lines
    lock the currency; removing the last one (with no transaction) unlocks it again."""
    _plan(team)
    owner = team.ctx("olga", "owner")
    assert workspace_settings(team.factory, owner).base_currency_locked is True
    with pytest.raises(workspaces.BaseCurrencyLocked):
        update_settings(team.factory, owner, **SETTINGS)
    with team.factory() as session:
        food = session.scalars(select(Category.id)).one()
    set_planned(
        team.factory, team.ctx("ed", "editor"), month=YearMonth(2026, 3), category_id=food,
        amount="0", locale="en",
    )  # fmt: skip
    assert workspace_settings(team.factory, owner).base_currency_locked is False
    assert update_settings(team.factory, owner, **SETTINGS).base_currency == "PLN"


def test_every_registered_lock_is_consulted(
    team: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert [
        workspace_has_transactions,
        workspace_has_budget_lines,
    ] == workspaces.BASE_CURRENCY_LOCKS
    # Any check that reports stored amounts locks it.
    monkeypatch.setattr(workspaces, "BASE_CURRENCY_LOCKS", [lambda _session, _ctx: True])
    with pytest.raises(workspaces.BaseCurrencyLocked):
        update_settings(team.factory, team.ctx("olga", "owner"), **SETTINGS)


def test_the_settings_write_locks_the_workspace_before_reading_it(team: WorkspaceDb) -> None:
    """PostgreSQL: the exclusive lock orders a currency change against a first transaction."""
    seen: list[str] = []

    def record(state: ORMExecuteState) -> None:
        statement = state.statement
        assert isinstance(statement, ClauseElement)
        seen.append(str(statement.compile(dialect=postgresql.dialect())))

    event.listen(Session, "do_orm_execute", record)
    try:
        update_settings(team.factory, team.ctx("olga", "owner"), **SETTINGS)
    finally:
        event.remove(Session, "do_orm_execute", record)
    workspace_reads = [i for i, s in enumerate(seen) if "FROM workspaces" in s]
    transaction_reads = [i for i, s in enumerate(seen) if "FROM transactions" in s]
    assert seen[workspace_reads[0]].rstrip().endswith("FOR UPDATE")
    assert transaction_reads and workspace_reads[0] < min(transaction_reads)


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
