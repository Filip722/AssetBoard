"""Months (F2.5): the current month, month state, closing and reopening, and the open-month guard.

Every member may read a month. Editors and owners may **close** a month that is not in the
future (current and past months; future months stay open for planning). Only owners may
**reopen** one. A closed month is read-only for everyone, owners included: entries and planned
values change only after an owner reopens it. Both actions are audited (``month.closed``,
``month.reopened``) and run in one ``BEGIN IMMEDIATE`` transaction that re-reads the actor's
role (``require_role_now``). Closing a closed month, or reopening an open one, changes nothing
and records nothing.

**The guard.** Every write to a month's data (F2.2 transactions, F2.3 budget lines, ...) calls
``ensure_month_open`` (or ``ensure_date_open``) inside its own write transaction, after
``require_role_now`` and before changing anything. It raises ``MonthClosed``. A write that reads
a stored row to learn its month (editing a transaction reads the old date) calls
``lock_month_writes`` before that read.

**Concurrency.** On SQLite every write runs under ``BEGIN IMMEDIATE``, so writers are serialised
and the guard sees every committed close. PostgreSQL does not queue writers at ``BEGIN``, so
the guard first takes a shared lock on the workspace row and close/reopen take an exclusive one
(``CurrentWorkspaceRepository.lock``): a close waits for writes in flight, and a write that
starts during a close waits for it and then sees the month closed. The workspace row is always
locked first, before any other row, so these locks cannot deadlock each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import User
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    MonthStateRepository,
)
from assetboard.domain.months import YearMonth
from assetboard.services.context import Context, Role, has_role, require_role, require_role_now


class MonthError(Exception):
    code = "invalid"


class MonthClosed(MonthError):
    """The month is closed: its entries and planned values cannot change until it is reopened."""

    code = "month_closed"

    def __init__(self, month: YearMonth) -> None:
        super().__init__(f"{month} is closed")
        self.month = month


class MonthInFuture(MonthError):
    """Only the current and past months can be closed."""

    code = "month_in_future"


@dataclass(frozen=True, slots=True)
class MonthView:
    month: YearMonth
    current: YearMonth  # today's month in the workspace time zone
    closed_at: datetime | None
    closed_by_user_id: UUID | None
    closed_by_name: str | None  # None while closed: the account no longer exists
    can_close: bool
    can_reopen: bool

    @property
    def closed(self) -> bool:
        return self.closed_at is not None

    @property
    def prev(self) -> YearMonth | None:
        return self.month.prev()

    @property
    def next(self) -> YearMonth | None:
        return self.month.next()

    @property
    def is_current(self) -> bool:
        return self.month == self.current

    @property
    def is_future(self) -> bool:
        return self.month > self.current


def current_month(ctx: Context) -> YearMonth:
    """Today's month in the workspace's time zone, from the injected clock."""
    return YearMonth.from_date(ctx.clock.today(ctx.workspace.timezone))


# The guard (called by other services inside their write transactions)


def lock_month_writes(session: Session, ctx: Context) -> None:
    """Take the shared workspace lock that orders month writes against close and reopen.

    A write whose month depends on a stored row (editing a transaction reads its old date) calls
    this **before reading that row**, then ``ensure_month_open`` for the old and the new month.
    Otherwise, on PostgreSQL, a concurrent move and close could both pass. Taking the lock again
    in the same transaction (as ``ensure_month_open`` does) is harmless. On SQLite the
    transaction's ``BEGIN IMMEDIATE`` already serialises writers.
    """
    CurrentWorkspaceRepository(session, ctx).lock(exclusive=False)


def ensure_month_open(session: Session, ctx: Context, month: YearMonth) -> None:
    """Raise ``MonthClosed`` if ``month`` is closed in the current workspace.

    Call inside the write transaction (``run_immediate``), after ``require_role_now`` and before
    any change. Takes the shared workspace lock (``lock_month_writes``) first; a write that reads
    rows to find its months must take that lock before those reads.
    """
    lock_month_writes(session, ctx)
    state = MonthStateRepository(session, ctx).for_month(month)
    if state is not None and state.closed_at is not None:
        raise MonthClosed(month)


def ensure_date_open(session: Session, ctx: Context, day: date) -> None:
    """``ensure_month_open`` for the month that contains ``day``."""
    ensure_month_open(session, ctx, YearMonth.from_date(day))


# Reads (every member)


def _display_name(session: Session, user_id: UUID | None) -> str | None:
    if user_id is None:
        return None
    return session.scalars(select(User.display_name).where(User.id == user_id)).one_or_none()


def get_month(factory: sessionmaker[Session], ctx: Context, month: YearMonth) -> MonthView:
    require_role(ctx, Role.VIEWER)
    current = current_month(ctx)
    with factory() as session:
        state = MonthStateRepository(session, ctx).for_month(month)
        closed_at = state.closed_at if state is not None else None
        closed_by = state.closed_by_user_id if state is not None and closed_at else None
        name = _display_name(session, closed_by)
    return MonthView(
        month=month,
        current=current,
        closed_at=closed_at,
        closed_by_user_id=closed_by,
        closed_by_name=name,
        can_close=closed_at is None and month <= current and has_role(ctx, Role.EDITOR),
        can_reopen=closed_at is not None and has_role(ctx, Role.OWNER),
    )


# Writes


def _audit(session: Session, ctx: Context, action: str, state_id: UUID, month: YearMonth) -> None:
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action=action,
        entity_type="month",
        entity_id=str(state_id),
        ts=ctx.clock.now(),
        data={"month": str(month)},
    )


def close_month(factory: sessionmaker[Session], ctx: Context, month: YearMonth) -> bool:
    """Close ``month`` (editors and owners). False when it was already closed."""
    require_role(ctx, Role.EDITOR)
    if month > current_month(ctx):
        raise MonthInFuture

    def write(session: Session) -> bool:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        repository = MonthStateRepository(session, ctx)
        state = repository.for_month(month) or repository.create(month)
        if state.closed_at is not None:
            return False
        repository.update(state.id, closed_at=ctx.clock.now(), closed_by_user_id=ctx.user.id)
        _audit(session, ctx, "month.closed", state.id, month)
        return True

    return run_immediate(factory, write)


def reopen_month(factory: sessionmaker[Session], ctx: Context, month: YearMonth) -> bool:
    """Reopen ``month`` (owners only). False when it was not closed."""
    require_role(ctx, Role.OWNER)

    def write(session: Session) -> bool:
        require_role_now(session, ctx, Role.OWNER)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        repository = MonthStateRepository(session, ctx)
        state = repository.for_month(month)
        if state is None or state.closed_at is None:
            return False
        repository.update(state.id, closed_at=None, closed_by_user_id=None)
        _audit(session, ctx, "month.reopened", state.id, month)
        return True

    return run_immediate(factory, write)
