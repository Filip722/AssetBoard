"""Months service (F2.5): close/reopen role rules, the open-month guard, audit and concurrency."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import ORMExecuteState, Session
from sqlalchemy.sql import ClauseElement

from assetboard.clock import FixedClock
from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import AuditEvent, Membership, User
from assetboard.db.models.ledger import MonthState
from assetboard.db.repositories import AuditEventRepository
from assetboard.domain.months import YearMonth
from assetboard.services import months as service
from assetboard.services.context import Context, PermissionDenied, Role, require_role_now
from assetboard.services.months import (
    MonthClosed,
    MonthInFuture,
    close_month,
    current_month,
    ensure_date_open,
    ensure_month_open,
    get_month,
    lock_month_writes,
    reopen_month,
)
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

SEPTEMBER, OCTOBER, NOVEMBER = YearMonth(2026, 9), YearMonth(2026, 10), YearMonth(2026, 11)


class TickingClock(FixedClock):
    """Each ``now()`` is one second later, so audit events sort in the order they were made."""

    def __init__(self) -> None:
        super().__init__("2026-10-14T12:00:00+00:00")  # today: October 2026
        self._ticks = 0

    def now(self) -> datetime:
        self._ticks += 1
        return super().now() + timedelta(seconds=self._ticks)


@pytest.fixture
def team(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for prepared in workspace_db(tmp_path):
        prepared.clock = TickingClock()
        prepared.add_member("olga", "owner")
        prepared.add_member("ed", "editor")
        prepared.add_member("vic", "viewer")
        yield prepared


def _events(db: WorkspaceDb) -> list[AuditEvent]:
    with db.factory() as session:
        statement = select(AuditEvent).where(AuditEvent.action.startswith("month."))
        return list(session.scalars(statement.order_by(AuditEvent.ts, AuditEvent.id)))


def probe_write(db: WorkspaceDb, ctx: Context, month: YearMonth) -> None:
    """What an F2.2/F2.3 write does: role re-check, the guard, then the change itself."""

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        ensure_month_open(session, ctx, month)
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="test.entry_written",
            entity_type="test",
            entity_id=str(month),
            ts=ctx.clock.now(),
            data={},
        )

    run_immediate(db.factory, write)


def _probe_writes(db: WorkspaceDb) -> int:
    with db.factory() as session:
        statement = select(AuditEvent.id).where(AuditEvent.action == "test.entry_written")
        return len(session.scalars(statement).all())


# Today's month


@pytest.mark.parametrize(
    ("instant", "zone", "expected"),
    [
        ("2026-10-31T23:30:00+00:00", "UTC", OCTOBER),
        ("2026-10-31T23:30:00+00:00", "Europe/Warsaw", NOVEMBER),  # already 1 November there
        ("2026-11-01T03:00:00+00:00", "America/New_York", OCTOBER),  # still 31 October there
        ("2026-12-31T23:59:59+00:00", "Asia/Tokyo", YearMonth(2027, 1)),
    ],
)
def test_current_month_uses_the_clock_in_the_workspace_time_zone(
    team: WorkspaceDb, instant: str, zone: str, expected: YearMonth
) -> None:
    ctx = team.ctx("vic", "viewer")
    ctx = replace(ctx, clock=FixedClock(instant), workspace=replace(ctx.workspace, timezone=zone))
    assert current_month(ctx) == expected


# Reading months


@pytest.mark.parametrize("month", [YearMonth(1900, 1), SEPTEMBER, OCTOBER, YearMonth(2199, 12)])
def test_empty_past_and_future_months_read_as_open(team: WorkspaceDb, month: YearMonth) -> None:
    view = get_month(team.factory, team.ctx("vic", "viewer"), month)
    assert (view.month, view.current, view.closed, view.closed_at) == (month, OCTOBER, False, None)
    assert (view.is_current, view.is_future) == (month == OCTOBER, month > OCTOBER)


def test_what_each_role_may_do_on_an_open_and_a_closed_month(team: WorkspaceDb) -> None:
    def abilities(key: str, role: str, month: YearMonth) -> tuple[bool, bool]:
        view = get_month(team.factory, team.ctx(key, role), month)
        return view.can_close, view.can_reopen

    assert abilities("olga", "owner", OCTOBER) == (True, False)
    assert abilities("ed", "editor", OCTOBER) == (True, False)
    assert abilities("vic", "viewer", OCTOBER) == (False, False)
    assert abilities("ed", "editor", NOVEMBER) == (False, False)  # future: stays open
    close_month(team.factory, team.ctx("ed", "editor"), OCTOBER)
    assert abilities("olga", "owner", OCTOBER) == (False, True)
    assert abilities("ed", "editor", OCTOBER) == (False, False)
    assert abilities("vic", "viewer", OCTOBER) == (False, False)


def test_a_closed_month_shows_when_and_by_whom_to_everyone(team: WorkspaceDb) -> None:
    close_month(team.factory, team.ctx("ed", "editor"), SEPTEMBER)
    view = get_month(team.factory, team.ctx("vic", "viewer"), SEPTEMBER)
    assert view.closed_at is not None and view.closed_at.date() == date(2026, 10, 14)
    assert (view.closed_by_user_id, view.closed_by_name) == (team.users["ed"], "Ed")
    assert not get_month(team.factory, team.ctx("vic", "viewer"), OCTOBER).closed


def test_a_deleted_closer_leaves_the_month_closed_without_a_name(team: WorkspaceDb) -> None:
    close_month(team.factory, team.ctx("ed", "editor"), SEPTEMBER)
    with team.factory.begin() as session:
        session.execute(delete(Membership).where(Membership.user_id == team.users["ed"]))
        session.execute(delete(User).where(User.id == team.users["ed"]))
    view = get_month(team.factory, team.ctx("vic", "viewer"), SEPTEMBER)
    assert view.closed and (view.closed_by_user_id, view.closed_by_name) == (None, None)


# Closing and reopening: roles


@pytest.mark.parametrize(("key", "role"), [("olga", "owner"), ("ed", "editor")])
def test_owners_and_editors_can_close(team: WorkspaceDb, key: str, role: str) -> None:
    assert close_month(team.factory, team.ctx(key, role), OCTOBER) is True
    assert get_month(team.factory, team.ctx("vic", "viewer"), OCTOBER).closed
    [closed] = _events(team)
    assert (closed.action, closed.actor_user_id, closed.data) == (
        "month.closed",
        team.users[key],
        {"month": "2026-10"},
    )
    assert closed.entity_type == "month" and closed.workspace_id == team.workspace_id


def test_viewers_cannot_close_or_reopen(team: WorkspaceDb) -> None:
    with pytest.raises(PermissionDenied):
        close_month(team.factory, team.ctx("vic", "viewer"), OCTOBER)
    close_month(team.factory, team.ctx("ed", "editor"), OCTOBER)
    with pytest.raises(PermissionDenied):
        reopen_month(team.factory, team.ctx("vic", "viewer"), OCTOBER)
    assert [e.action for e in _events(team)] == ["month.closed"]


def test_only_owners_can_reopen(team: WorkspaceDb) -> None:
    close_month(team.factory, team.ctx("ed", "editor"), OCTOBER)
    with pytest.raises(PermissionDenied) as caught:
        reopen_month(team.factory, team.ctx("ed", "editor"), OCTOBER)
    assert caught.value.required is Role.OWNER
    assert get_month(team.factory, team.ctx("vic", "viewer"), OCTOBER).closed
    assert reopen_month(team.factory, team.ctx("olga", "owner"), OCTOBER) is True
    view = get_month(team.factory, team.ctx("vic", "viewer"), OCTOBER)
    assert not view.closed and view.closed_by_user_id is None
    assert [(e.action, e.actor_user_id) for e in _events(team)] == [
        ("month.closed", team.users["ed"]),
        ("month.reopened", team.users["olga"]),
    ]
    with team.factory() as session:  # the row is kept, its closed fields cleared
        [row] = session.scalars(select(MonthState)).all()
        assert (row.month, row.closed_at, row.closed_by_user_id) == (date(2026, 10, 1), None, None)


@pytest.mark.parametrize(
    ("action", "stored", "claimed"),
    [("close", "viewer", "editor"), ("reopen", "editor", "owner")],
)
def test_a_stale_context_is_refused_inside_the_transaction(
    team: WorkspaceDb, action: str, stored: str, claimed: str
) -> None:
    close_month(team.factory, team.ctx("olga", "owner"), SEPTEMBER)
    team.add_member("dee", stored)  # demoted: the Context still claims the higher role
    operation = close_month if action == "close" else reopen_month
    with pytest.raises(PermissionDenied):
        operation(
            team.factory, team.ctx("dee", claimed), SEPTEMBER if action == "reopen" else OCTOBER
        )
    assert len(_events(team)) == 1


@pytest.mark.parametrize("action", ["close", "reopen"])
def test_the_role_is_checked_before_any_database_work(
    team: WorkspaceDb, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    monkeypatch.setattr(service, "run_immediate", lambda *_args: pytest.fail("no write"))
    operation = close_month if action == "close" else reopen_month
    with pytest.raises(PermissionDenied):
        operation(team.factory, team.ctx("vic", "viewer"), OCTOBER)


def test_a_removed_member_cannot_close(team: WorkspaceDb) -> None:
    with team.factory.begin() as session:
        session.execute(delete(Membership).where(Membership.user_id == team.users["ed"]))
    with pytest.raises(PermissionDenied):
        close_month(team.factory, team.ctx("ed", "editor"), OCTOBER)


# Closing and reopening: rules


@pytest.mark.parametrize("month", [NOVEMBER, YearMonth(2027, 1), YearMonth(2199, 12)])
def test_future_months_cannot_be_closed(team: WorkspaceDb, month: YearMonth) -> None:
    with pytest.raises(MonthInFuture) as caught:
        close_month(team.factory, team.ctx("olga", "owner"), month)
    assert caught.value.code == "month_in_future"
    assert _events(team) == []


def test_the_future_is_judged_in_the_workspace_time_zone(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    late = FixedClock("2026-10-31T23:30:00+00:00")  # 1 November in Warsaw
    in_utc = replace(ctx, clock=late)
    in_warsaw = replace(in_utc, workspace=replace(ctx.workspace, timezone="Europe/Warsaw"))
    with pytest.raises(MonthInFuture):
        close_month(team.factory, in_utc, NOVEMBER)
    assert close_month(team.factory, in_warsaw, NOVEMBER) is True


def test_past_months_can_be_closed_far_back(team: WorkspaceDb) -> None:
    assert close_month(team.factory, team.ctx("ed", "editor"), YearMonth(1900, 1)) is True


def test_closing_twice_and_reopening_an_open_month_change_nothing(team: WorkspaceDb) -> None:
    assert reopen_month(team.factory, team.ctx("olga", "owner"), OCTOBER) is False  # no row
    assert close_month(team.factory, team.ctx("ed", "editor"), OCTOBER) is True
    first = get_month(team.factory, team.ctx("vic", "viewer"), OCTOBER)
    later = replace(team.ctx("olga", "owner"), clock=FixedClock("2026-10-20T08:00:00+00:00"))
    assert close_month(team.factory, later, OCTOBER) is False
    assert get_month(team.factory, team.ctx("vic", "viewer"), OCTOBER) == first  # same closer
    assert reopen_month(team.factory, team.ctx("olga", "owner"), OCTOBER) is True
    assert reopen_month(team.factory, team.ctx("olga", "owner"), OCTOBER) is False  # open row
    assert close_month(team.factory, team.ctx("ed", "editor"), OCTOBER) is True  # reuses it
    assert [e.action for e in _events(team)] == ["month.closed", "month.reopened", "month.closed"]
    with team.factory() as session:
        assert len(session.scalars(select(MonthState)).all()) == 1


def test_months_are_closed_per_workspace(team: WorkspaceDb) -> None:
    other = team.add_workspace("Other")
    team.add_member("ed", "editor", other)
    close_month(team.factory, team.ctx("ed", "editor", other), OCTOBER)
    assert get_month(team.factory, team.ctx("ed", "editor", other), OCTOBER).closed
    assert not get_month(team.factory, team.ctx("ed", "editor"), OCTOBER).closed
    probe_write(team, team.ctx("ed", "editor"), OCTOBER)  # this workspace's October is open
    with pytest.raises(MonthClosed):
        probe_write(team, team.ctx("ed", "editor", other), OCTOBER)


# The guard: enforcement through the service API


@pytest.mark.parametrize(("key", "role"), [("olga", "owner"), ("ed", "editor")])
def test_writes_to_a_closed_month_are_refused_for_everyone(
    team: WorkspaceDb, key: str, role: str
) -> None:
    close_month(team.factory, team.ctx("ed", "editor"), OCTOBER)
    with pytest.raises(MonthClosed) as caught:
        probe_write(team, team.ctx(key, role), OCTOBER)  # owners too: reopen first
    assert (caught.value.month, caught.value.code) == (OCTOBER, "month_closed")
    assert _probe_writes(team) == 0
    probe_write(team, team.ctx(key, role), SEPTEMBER)  # other months stay open
    probe_write(team, team.ctx(key, role), NOVEMBER)
    assert _probe_writes(team) == 2


def test_reopening_allows_writes_again(team: WorkspaceDb) -> None:
    close_month(team.factory, team.ctx("ed", "editor"), OCTOBER)
    reopen_month(team.factory, team.ctx("olga", "owner"), OCTOBER)
    probe_write(team, team.ctx("ed", "editor"), OCTOBER)
    assert _probe_writes(team) == 1


def test_the_guard_sees_a_close_made_after_the_writers_context_was_built(
    team: WorkspaceDb,
) -> None:
    writer = team.ctx("ed", "editor")
    view = get_month(team.factory, writer, OCTOBER)  # the page showed the month open
    assert not view.closed
    close_month(team.factory, team.ctx("olga", "owner"), OCTOBER)
    with pytest.raises(MonthClosed):
        probe_write(team, writer, OCTOBER)


@pytest.mark.parametrize(
    ("day", "closed"),
    [(date(2026, 10, 1), True), (date(2026, 10, 31), True), (date(2026, 11, 1), False)],
)
def test_ensure_date_open_checks_the_month_of_the_date(
    team: WorkspaceDb, day: date, closed: bool
) -> None:
    close_month(team.factory, team.ctx("ed", "editor"), OCTOBER)
    ctx = team.ctx("ed", "editor")

    def check(session: Session) -> None:
        ensure_date_open(session, ctx, day)

    if closed:
        with pytest.raises(MonthClosed):
            run_immediate(team.factory, check)
    else:
        run_immediate(team.factory, check)


# Concurrency


def test_a_close_waits_for_a_write_in_flight_and_later_writes_are_refused(
    team: WorkspaceDb,
) -> None:
    """SQLite: BEGIN IMMEDIATE queues the close behind a write that already passed the guard."""
    inside, release = threading.Event(), threading.Event()
    writer = team.ctx("ed", "editor")
    finished: list[str] = []

    def slow_write(session: Session) -> None:
        require_role_now(session, writer, Role.EDITOR)
        ensure_month_open(session, writer, OCTOBER)
        inside.set()
        assert release.wait(5)
        AuditEventRepository(session, writer).record(
            actor_user_id=writer.user.id,
            action="test.entry_written",
            entity_type="test",
            entity_id="2026-10",
            ts=writer.clock.now(),
            data={},
        )
        finished.append("write")

    def close() -> None:
        close_month(team.factory, team.ctx("olga", "owner"), OCTOBER)
        finished.append("close")

    write_thread = threading.Thread(target=run_immediate, args=(team.factory, slow_write))
    write_thread.start()
    assert inside.wait(5)
    close_thread = threading.Thread(target=close)
    close_thread.start()
    time.sleep(0.2)
    assert finished == []  # the close is queued behind the write
    release.set()
    write_thread.join(5)
    close_thread.join(5)
    assert finished == ["write", "close"]
    assert _probe_writes(team) == 1
    with pytest.raises(MonthClosed):
        probe_write(team, writer, OCTOBER)


def _locks(team: WorkspaceDb, operation: Callable[[], Any]) -> list[str]:
    """The statements ``operation`` runs, compiled for PostgreSQL (where the locks apply)."""
    seen: list[str] = []

    def record(state: ORMExecuteState) -> None:
        statement = state.statement
        assert isinstance(statement, ClauseElement)
        seen.append(str(statement.compile(dialect=postgresql.dialect())))

    event.listen(Session, "do_orm_execute", record)
    try:
        operation()
    finally:
        event.remove(Session, "do_orm_execute", record)
    return seen


def test_lock_month_writes_takes_the_share_lock_and_can_be_taken_again(
    team: WorkspaceDb,
) -> None:
    """The lock a write takes before reading the rows whose months it then checks (F2.2 edits)."""
    editor = team.ctx("ed", "editor")

    def twice_then_guard(session: Session) -> None:
        lock_month_writes(session, editor)
        lock_month_writes(session, editor)  # harmless in the same transaction
        ensure_month_open(session, editor, OCTOBER)  # takes it a third time

    statements = _locks(team, lambda: run_immediate(team.factory, twice_then_guard))
    locks = [s for s in statements if "FROM workspaces" in s]
    assert len(locks) == 3 and all(s.rstrip().endswith("FOR SHARE") for s in locks)
    assert statements.index(locks[0]) == 0  # nothing is read before the first lock


def test_the_workspace_row_is_locked_before_month_states_are_read(team: WorkspaceDb) -> None:
    """PostgreSQL: the guard takes a shared lock, close and reopen an exclusive one, first."""
    editor, owner = team.ctx("ed", "editor"), team.ctx("olga", "owner")

    def first_lock_then_month_state(statements: list[str], clause: str) -> None:
        lock = next(i for i, s in enumerate(statements) if "FROM workspaces" in s and "FOR" in s)
        assert statements[lock].rstrip().endswith(clause)
        month_reads = [i for i, s in enumerate(statements) if "month_states" in s]
        assert month_reads and lock < min(month_reads)

    guard = _locks(
        team, lambda: run_immediate(team.factory, lambda s: ensure_month_open(s, editor, OCTOBER))
    )
    first_lock_then_month_state(guard, "FOR SHARE")
    closing = _locks(team, lambda: close_month(team.factory, editor, OCTOBER))
    first_lock_then_month_state(closing, "FOR UPDATE")
    reopening = _locks(team, lambda: reopen_month(team.factory, owner, OCTOBER))
    first_lock_then_month_state(reopening, "FOR UPDATE")


def test_another_workspaces_closed_state_is_never_read(team: WorkspaceDb) -> None:
    other = team.add_workspace("Other")
    with team.factory.begin() as session:  # planted directly: a closed October elsewhere
        session.add(
            MonthState(
                id=uuid4(),
                workspace_id=other,
                month=date(2026, 10, 1),
                closed_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
        )
    assert not get_month(team.factory, team.ctx("vic", "viewer"), OCTOBER).closed
    probe_write(team, team.ctx("ed", "editor"), OCTOBER)
    assert _probe_writes(team) == 1
