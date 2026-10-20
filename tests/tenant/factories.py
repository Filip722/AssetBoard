"""One row factory per workspace-owned model, for the tenant-isolation test.

A factory builds an unsaved row for the requested workspace (creating any non-workspace rows it
needs, such as users). ``update_values`` is a valid change for the cross-tenant update check.
Adding a workspace-owned model without an entry here fails
``test_discovery_finds_every_workspace_owned_model_with_repository_and_factory``.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from assetboard.db.models.identity import AuditEvent, Invitation, Membership, User
from assetboard.db.models.ledger import BudgetLine, Category, MonthState, Transaction
from assetboard.db.models.scheduled import ScheduledItem, ScheduledOccurrence
from assetboard.services.invitations import new_token


@dataclass(frozen=True)
class TenantCase:
    factory: Callable[[Session, UUID], Any]
    update_values: Mapping[str, object] = field(default_factory=dict[str, object])
    # Operations the repository must refuse; asserted against the repository's own declaration.
    unsupported: frozenset[str] = frozenset()


def _user(session: Session) -> User:
    user = User(email=f"member-{uuid4().hex[:12]}@example.com", display_name="Member")
    session.add(user)
    session.flush()
    return user


def make_membership(session: Session, workspace_id: UUID) -> Membership:
    return Membership(workspace_id=workspace_id, user_id=_user(session).id, role="viewer")


def make_audit_event(session: Session, workspace_id: UUID) -> AuditEvent:
    return AuditEvent(
        id=uuid4(),
        workspace_id=workspace_id,
        actor_type="user",
        action="test.created",
        entity_type="test",
        entity_id=uuid4().hex,
        data={"synthetic": True},
        ts=session.info["clock"].now(),
    )


def make_invitation(session: Session, workspace_id: UUID) -> Invitation:
    _token, token_hash = new_token()
    now = session.info["clock"].now()
    return Invitation(
        workspace_id=workspace_id,
        token_hash=token_hash,
        role="viewer",
        email=None,
        expires_at=now + timedelta(days=7),
        failed_email_attempts=0,
        created_by_user_id=_user(session).id,
    )


def make_category(session: Session, workspace_id: UUID) -> Category:
    name = f"Category {uuid4().hex[:8]}"
    return Category(
        workspace_id=workspace_id,
        name=name,
        name_key=name.casefold(),
        kind="expense",
        treatment="regular",
        color="#16a34a",
        sort_order=0,
    )


# (workspace, month) is unique: each row gets its own month.
_MONTHS = itertools.count()


def make_month_state(session: Session, workspace_id: UUID) -> MonthState:
    index = next(_MONTHS)
    return MonthState(workspace_id=workspace_id, month=date(2000 + index // 12, 1 + index % 12, 1))


def make_transaction(session: Session, workspace_id: UUID) -> Transaction:
    """A transaction with its own category in the same workspace (composite foreign key)."""
    category = make_category(session, workspace_id)
    session.add(category)
    session.flush()
    return Transaction(
        workspace_id=workspace_id,
        direction="expense",
        date=date(2026, 10, 7),
        amount_minor=1250,
        currency="EUR",
        category_id=category.id,
        description="Groceries",
        description_key="groceries",
        source="manual",
        created_by_user_id=None,
    )


def make_budget_line(session: Session, workspace_id: UUID) -> BudgetLine:
    """A planned value with its own category in the same workspace (composite foreign key)."""
    category = make_category(session, workspace_id)
    session.add(category)
    session.flush()
    return BudgetLine(
        workspace_id=workspace_id,
        month=date(2026, 10, 1),
        category_id=category.id,
        planned_minor=55000,
        currency="EUR",
    )


def make_scheduled_item(session: Session, workspace_id: UUID) -> ScheduledItem:
    category = make_category(session, workspace_id)
    session.add(category)
    session.flush()
    return ScheduledItem(
        workspace_id=workspace_id,
        name="Insurance",
        direction="expense",
        category_id=category.id,
        amount_mode="fixed",
        amount_minor=180000,
        currency="EUR",
        start_date=date(2026, 1, 1),
        freq="monthly",
        interval=1,
        weekdays=[],
        month_day="14",
        month=1,
        weekend_policy="none",
        notes="",
    )


def make_scheduled_occurrence(session: Session, workspace_id: UUID) -> ScheduledOccurrence:
    item = make_scheduled_item(session, workspace_id)
    session.add(item)
    session.flush()
    return ScheduledOccurrence(
        workspace_id=workspace_id,
        scheduled_item_id=item.id,
        category_id=item.category_id,
        direction="expense",
        due_date=date(2026, 3, 14),
        expected_minor=180000,
        currency="EUR",
        amount_state="confirmed",
        status="pending",
        label="Insurance",
    )


TENANT_CASES: dict[type[Any], TenantCase] = {
    ScheduledItem: TenantCase(make_scheduled_item, update_values={"name": "Rent"}),
    ScheduledOccurrence: TenantCase(make_scheduled_occurrence, update_values={"label": "Rent"}),
    Membership: TenantCase(make_membership, update_values={"role": "editor"}),
    AuditEvent: TenantCase(make_audit_event, unsupported=frozenset({"update", "delete"})),
    # Invitations change only through named transitions (revoke, mark_used, ...).
    Invitation: TenantCase(make_invitation, unsupported=frozenset({"update", "delete"})),
    Category: TenantCase(make_category, update_values={"color": "#e11d48"}),
    # Reopening clears the closed fields; rows are never deleted.
    MonthState: TenantCase(
        make_month_state,
        update_values={"closed_at": datetime(2026, 1, 31, 12, tzinfo=UTC)},
        unsupported=frozenset({"delete"}),
    ),
    # Soft delete only (``deleted_at`` through ``update``); rows are never removed.
    Transaction: TenantCase(
        make_transaction,
        update_values={"description": "Market", "description_key": "market"},
        unsupported=frozenset({"delete"}),
    ),
    # A zero plan removes the line (generic delete); values change through update.
    BudgetLine: TenantCase(make_budget_line, update_values={"planned_minor": 999}),
}
