"""Planned values (F2.3): upsert per (month, category), removal on zero, copying a plan, the
automatic copy, roles, the closed-month guard, audit and the seams used by other services."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import ORMExecuteState, Session
from sqlalchemy.sql import ClauseElement

from assetboard.clock import FixedClock
from assetboard.db.models.identity import AuditEvent, Workspace
from assetboard.db.models.ledger import BudgetLine, MonthState
from assetboard.db.repositories import NotFound
from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.services import budget as service
from assetboard.services.budget import (
    CopyResult,
    InvalidPlannedValue,
    SavedLine,
    auto_copy_on_open,
    category_usage,
    copy_previous_plan,
    get_plan,
    set_planned,
    workspace_has_budget_lines,
)
from assetboard.services.categories import (
    archive_category,
    create_category,
    delete_category,
    get_category,
    update_category,
)
from assetboard.services.category_usage import CategoryUsage
from assetboard.services.context import Context, PermissionDenied, Role
from assetboard.services.months import MonthClosed, close_month, reopen_month
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

AUGUST, SEPTEMBER, OCTOBER = YearMonth(2026, 8), YearMonth(2026, 9), YearMonth(2026, 10)
NOVEMBER = YearMonth(2026, 11)
START = datetime(2026, 10, 14, 12, tzinfo=UTC)  # the current month is October 2026


class SteppingClock(FixedClock):
    def __init__(self) -> None:
        super().__init__(START.isoformat())
        self.at = START

    def now(self) -> datetime:
        self.at += timedelta(microseconds=1)
        return self.at


@pytest.fixture
def team(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for prepared in workspace_db(tmp_path):
        clock = SteppingClock()
        prepared.clock = clock
        prepared.factory.configure(info={"clock": clock})
        prepared.add_member("olga", "owner")
        prepared.add_member("ed", "editor")
        prepared.add_member("vic", "viewer")
        yield prepared


def _category(
    db: WorkspaceDb, name: str, kind: str = "expense", treatment: str = "regular"
) -> UUID:
    return create_category(
        db.factory, db.ctx("ed", "editor"), name=name, kind=kind, treatment=treatment,
        color="#16a34a",
    ).id  # fmt: skip


def _set(
    db: WorkspaceDb,
    category_id: UUID,
    amount: str,
    *,
    month: YearMonth = OCTOBER,
    ctx: Context | None = None,
    locale: str = "pl",
) -> SavedLine:
    return set_planned(
        db.factory, ctx or db.ctx("ed", "editor"), month=month, category_id=category_id,
        amount=amount, locale=locale,
    )  # fmt: skip


def _planned(db: WorkspaceDb, month: YearMonth = OCTOBER) -> dict[UUID, int]:
    plan = get_plan(db.factory, db.ctx("vic", "viewer"), month)
    return {
        row.category.id: row.planned.amount_minor
        for group in plan.groups
        for row in group.rows
        if row.planned is not None
    }


def _lines(db: WorkspaceDb) -> list[BudgetLine]:
    with db.factory() as session:
        return list(session.scalars(select(BudgetLine)))


def _events(db: WorkspaceDb, prefix: str = "budget_line.") -> list[AuditEvent]:
    with db.factory() as session:
        statement = select(AuditEvent).where(AuditEvent.action.startswith(prefix))
        return list(session.scalars(statement.order_by(AuditEvent.ts, AuditEvent.id)))


def _invalid(operation: Callable[[], Any]) -> str:
    with pytest.raises(InvalidPlannedValue) as caught:
        operation()
    return caught.value.code


def _close(db: WorkspaceDb, month: YearMonth) -> None:
    close_month(db.factory, db.ctx("olga", "owner"), month)


def _auto(db: WorkspaceDb, on: bool = True) -> None:
    with db.factory.begin() as session:
        workspace = session.get(Workspace, db.workspace_id)
        assert workspace is not None
        workspace.auto_copy_plan = on


# Upsert per (month, category); zero or empty removes


def test_set_creates_then_updates_one_line_per_month_and_category(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    assert _set(team, food, "550") == SavedLine("created", food, Money(55000, "EUR"))
    first = _lines(team)
    assert _set(team, food, "1 234,56") == SavedLine("updated", food, Money(123456, "EUR"))
    lines = _lines(team)
    assert len(lines) == 1 and lines[0].id == first[0].id
    assert (lines[0].month, lines[0].planned_minor, lines[0].currency) == (
        date(2026, 10, 1),
        123456,
        "EUR",
    )
    _set(team, food, "100", month=NOVEMBER)  # another month: its own line
    assert _planned(team) == {food: 123456}
    assert _planned(team, NOVEMBER) == {food: 10000}


def test_the_database_allows_one_line_per_month_and_category(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    _set(team, food, "1")
    with pytest.raises(IntegrityError), team.factory.begin() as session:
        session.add(
            BudgetLine(
                workspace_id=team.workspace_id,
                month=date(2026, 10, 1),
                category_id=food,
                planned_minor=2,
                currency="EUR",
            )  # fmt: skip
        )


@pytest.mark.parametrize("amount", ["", "   ", "0", "0,00", "-0"])
def test_zero_or_empty_removes_the_line(team: WorkspaceDb, amount: str) -> None:
    food = _category(team, "Food")
    _set(team, food, "550")
    assert _set(team, food, amount) == SavedLine("removed", food, None)
    assert _lines(team) == []
    assert _set(team, food, amount) == SavedLine("unchanged", food, None)  # nothing to remove
    assert [e.action for e in _events(team)] == ["budget_line.created", "budget_line.deleted"]


def test_saving_the_stored_value_changes_and_records_nothing(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    _set(team, food, "550")
    assert _set(team, food, "550,00").outcome == "unchanged"
    assert len(_events(team)) == 1


@pytest.mark.parametrize(
    ("amount", "code"),
    [
        ("abc", "invalid_amount"),
        ("-5", "amount_negative"),
        ("12,345", "too_many_decimals"),
        ("10000000000000", "amount_too_large"),
    ],
)
def test_invalid_values_are_refused_before_any_write(
    team: WorkspaceDb, amount: str, code: str
) -> None:
    food = _category(team, "Food")
    assert _invalid(lambda: _set(team, food, amount)) == code
    assert _lines(team) == [] and _events(team) == []


def test_values_are_parsed_in_the_callers_locale(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    assert _set(team, food, "1,234.56", locale="en").planned == Money(123456, "EUR")
    assert _set(team, food, "12,5", locale="pl").planned == Money(1250, "EUR")


def test_the_currency_is_read_from_the_workspace_not_the_context(team: WorkspaceDb) -> None:
    with team.factory.begin() as session:
        workspace = session.get(Workspace, team.workspace_id)
        assert workspace is not None
        workspace.base_currency = "JPY"
    food = _category(team, "Food")
    assert _set(team, food, "1500").planned == Money(1500, "JPY")  # the Context says EUR
    assert _invalid(lambda: _set(team, food, "1,5")) == "too_many_decimals"


def test_currency_checks_use_the_stored_currency_even_before_the_write(
    team: WorkspaceDb,
) -> None:
    """The Context says EUR (two decimals); the workspace row says KWD (three): a value with
    three decimals is valid, and nothing is judged by the Context's currency."""
    with team.factory.begin() as session:
        workspace = session.get(Workspace, team.workspace_id)
        assert workspace is not None
        workspace.base_currency = "KWD"
    food = _category(team, "Food")
    assert _set(team, food, "1,234").planned == Money(1234, "KWD")
    assert _invalid(lambda: _set(team, food, "1,2345")) == "too_many_decimals"
    assert _invalid(lambda: _set(team, food, "-1")) == "amount_negative"  # syntax and sign: early


def test_archived_and_transfer_categories_take_no_new_lines_but_keep_old_ones(
    team: WorkspaceDb,
) -> None:
    food, moving = _category(team, "Food"), _category(team, "To savings", treatment="transfer")
    assert _invalid(lambda: _set(team, moving, "100")) == "category_transfer"
    _set(team, food, "550")
    archive_category(team.factory, team.ctx("ed", "editor"), food)
    assert _set(team, food, "600").outcome == "updated"  # an existing line can change
    assert _invalid(lambda: _set(team, food, "1", month=NOVEMBER)) == "category_archived"
    assert _set(team, food, "0").outcome == "removed"  # ... and be removed


def test_an_unknown_or_foreign_category_is_not_found(team: WorkspaceDb) -> None:
    other = team.add_workspace("Other")
    team.add_member("ed", "editor", other)
    foreign = create_category(
        team.factory, team.ctx("ed", "editor", other), name="Food", kind="expense",
        treatment="regular", color="#16a34a",
    ).id  # fmt: skip
    for category_id in (foreign, uuid4()):
        with pytest.raises(NotFound):
            _set(team, category_id, "1")
    assert _lines(team) == []


def test_writes_are_audited_with_amounts(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    _set(team, food, "550")
    _set(team, food, "600,50")
    _set(team, food, "")
    created, updated, deleted = (e.data for e in _events(team))
    base = {"month": "2026-10", "category": "Food", "currency": "EUR"}
    assert created == base | {"amount": "550.00"}
    assert updated == base | {"amount": "600.50", "previous_amount": "550.00"}
    assert deleted == base | {"amount": "600.50"}
    assert {e.entity_type for e in _events(team)} == {"budget_line"}


# The month's plan (every member)


def test_the_plan_groups_categories_like_the_summary(team: WorkspaceDb) -> None:
    food, eating = _category(team, "Food"), _category(team, "Eating out")
    savings = _category(team, "Savings", treatment="savings")
    salary = _category(team, "Salary", kind="income")
    withdrawal = _category(team, "Withdrawal", kind="income", treatment="savings")
    moving = _category(team, "Between accounts", treatment="transfer")
    gone = _category(team, "Old")
    archive_category(team.factory, team.ctx("ed", "editor"), gone)
    _set(team, eating, "100")
    plan = get_plan(team.factory, team.ctx("vic", "viewer"), OCTOBER)
    assert [(g.key, [r.category.id for r in g.rows]) for g in plan.groups] == [
        ("expense", [food, eating]),
        ("saved", [savings]),
        ("income", [salary]),
        ("withdrawn", [withdrawal]),
    ]  # no transfer group without lines; archived categories without a line are left out
    assert plan.has_lines and plan.previous == SEPTEMBER
    assert moving not in {r.category.id for g in plan.groups for r in g.rows}


def test_archived_and_transfer_lines_stay_visible_so_they_can_be_removed(
    team: WorkspaceDb,
) -> None:
    food, rent = _category(team, "Food"), _category(team, "Rent")
    _set(team, food, "550")
    _set(team, rent, "1000")
    archive_category(team.factory, team.ctx("ed", "editor"), food)
    update_category(
        team.factory, team.ctx("ed", "editor"), rent, name="Rent", kind="expense",
        treatment="transfer", color="#16a34a",
    )  # fmt: skip
    plan = get_plan(team.factory, team.ctx("vic", "viewer"), OCTOBER)
    expense, transfer = plan.group("expense"), plan.group("transfer")
    assert expense is not None and transfer is not None
    assert [(r.category.id, r.category.archived) for r in expense.rows] == [(food, True)]
    assert [r.category.id for r in transfer.rows] == [rent]


# Copying the previous month's plan


def test_copy_copies_every_line_of_the_previous_month(team: WorkspaceDb) -> None:
    food, salary = _category(team, "Food"), _category(team, "Salary", kind="income")
    _set(team, food, "550", month=SEPTEMBER)
    _set(team, salary, "3000", month=SEPTEMBER)
    plan = get_plan(team.factory, team.ctx("ed", "editor"), OCTOBER)
    assert (plan.has_lines, plan.copyable_from_previous) == (False, 2)
    result = copy_previous_plan(team.factory, team.ctx("ed", "editor"), OCTOBER)
    assert result == CopyResult(source=SEPTEMBER, copied=2, skipped=0, kept=0)
    assert _planned(team) == {food: 55000, salary: 300000}
    copied = _events(team, "budget_line.copied")
    assert [e.data for e in copied] == [
        {"month": "2026-10", "source_month": "2026-09", "count": 2, "skipped": 0,
         "automatic": False}
    ]  # fmt: skip
    with team.factory() as session:
        state = session.scalars(select(MonthState).where(MonthState.month == date(2026, 10, 1)))
        state_id = state.one().id
    assert (copied[0].entity_type, copied[0].entity_id) == ("month", str(state_id))


def test_copy_never_overwrites_existing_lines(team: WorkspaceDb) -> None:
    food, rent = _category(team, "Food"), _category(team, "Rent")
    _set(team, food, "550", month=SEPTEMBER)
    _set(team, rent, "1000", month=SEPTEMBER)
    _set(team, food, "700")  # October already plans Food
    result = copy_previous_plan(team.factory, team.ctx("ed", "editor"), OCTOBER)
    assert result == CopyResult(source=SEPTEMBER, copied=1, skipped=0, kept=1)
    assert _planned(team) == {food: 70000, rent: 100000}
    again = copy_previous_plan(team.factory, team.ctx("ed", "editor"), OCTOBER)
    assert again == CopyResult(source=SEPTEMBER, copied=0, skipped=0, kept=2)
    assert len(_events(team, "budget_line.copied")) == 1  # nothing copied, nothing recorded


def test_copy_skips_archived_and_transfer_categories(team: WorkspaceDb) -> None:
    food, old, rent = _category(team, "Food"), _category(team, "Old"), _category(team, "Rent")
    for category in (food, old, rent):
        _set(team, category, "10", month=SEPTEMBER)
    archive_category(team.factory, team.ctx("ed", "editor"), old)
    update_category(
        team.factory, team.ctx("ed", "editor"), rent, name="Rent", kind="expense",
        treatment="transfer", color="#16a34a",
    )  # fmt: skip
    assert get_plan(team.factory, team.ctx("ed", "editor"), OCTOBER).copyable_from_previous == 1
    result = copy_previous_plan(team.factory, team.ctx("ed", "editor"), OCTOBER)
    assert result == CopyResult(source=SEPTEMBER, copied=1, skipped=2, kept=0)
    assert _planned(team) == {food: 1000}


def test_copy_is_strictly_from_the_immediately_previous_month(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    _set(team, food, "550", month=AUGUST)  # September is empty
    plan = get_plan(team.factory, team.ctx("ed", "editor"), OCTOBER)
    assert plan.copyable_from_previous == 0
    result = copy_previous_plan(team.factory, team.ctx("ed", "editor"), OCTOBER)
    assert result is not None and result.copied == 0 and _planned(team) == {}


def test_the_first_month_of_the_range_has_nothing_to_copy(team: WorkspaceDb) -> None:
    first = YearMonth(1900, 1)
    assert copy_previous_plan(team.factory, team.ctx("ed", "editor"), first) is None
    assert get_plan(team.factory, team.ctx("ed", "editor"), first).previous is None


def test_concurrent_copies_never_duplicate_lines(team: WorkspaceDb) -> None:
    food, rent = _category(team, "Food"), _category(team, "Rent")
    _set(team, food, "1", month=SEPTEMBER)
    _set(team, rent, "2", month=SEPTEMBER)
    results: list[CopyResult | None] = []
    barrier = threading.Barrier(4)

    def run() -> None:
        barrier.wait()
        results.append(copy_previous_plan(team.factory, team.ctx("ed", "editor"), OCTOBER))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(r.copied for r in results if r) == [0, 0, 0, 2]
    assert len(_lines(team)) == 4


# The automatic copy when a month is first opened


def _september_plan(db: WorkspaceDb) -> UUID:
    food = _category(db, "Food")
    _set(db, food, "550", month=SEPTEMBER)
    return food


def test_the_automatic_copy_is_off_by_default(team: WorkspaceDb) -> None:
    _september_plan(team)
    assert auto_copy_on_open(team.factory, team.ctx("ed", "editor"), OCTOBER) is None
    assert _planned(team) == {}


def test_the_automatic_copy_runs_once_per_month(team: WorkspaceDb) -> None:
    food = _september_plan(team)
    _auto(team)
    result = auto_copy_on_open(team.factory, team.ctx("ed", "editor"), OCTOBER)
    assert result == CopyResult(source=SEPTEMBER, copied=1, skipped=0, kept=0)
    assert _planned(team) == {food: 55000}
    assert _events(team, "budget_line.copied")[-1].data["automatic"] is True
    _set(team, food, "")  # emptied on purpose
    assert auto_copy_on_open(team.factory, team.ctx("ed", "editor"), OCTOBER) is None
    assert _planned(team) == {}


def test_a_month_planned_by_hand_is_never_copied_into(team: WorkspaceDb) -> None:
    food = _september_plan(team)
    rent = _category(team, "Rent")
    _set(team, rent, "1")
    _set(team, rent, "0")  # October had a plan once
    _auto(team)
    assert auto_copy_on_open(team.factory, team.ctx("ed", "editor"), OCTOBER) is None
    assert food not in _planned(team)


def test_the_automatic_copy_applies_to_current_and_later_months_only(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    _set(team, food, "1", month=AUGUST)
    _set(team, food, "2", month=OCTOBER)
    _auto(team)
    assert auto_copy_on_open(team.factory, team.ctx("ed", "editor"), SEPTEMBER) is None  # past
    result = auto_copy_on_open(team.factory, team.ctx("ed", "editor"), NOVEMBER)  # future
    assert result is not None and _planned(team, NOVEMBER) == {food: 200}


def test_the_automatic_copy_skips_viewers_and_closed_months(team: WorkspaceDb) -> None:
    _september_plan(team)
    _auto(team)
    assert auto_copy_on_open(team.factory, team.ctx("vic", "viewer"), OCTOBER) is None
    team.add_member("dee", "viewer")  # a stale editor Context
    assert auto_copy_on_open(team.factory, team.ctx("dee", "editor"), OCTOBER) is None
    _close(team, OCTOBER)
    assert auto_copy_on_open(team.factory, team.ctx("olga", "owner"), OCTOBER) is None
    assert _planned(team) == {}


def test_an_empty_previous_month_writes_nothing_so_a_later_visit_can_copy(
    team: WorkspaceDb,
) -> None:
    _auto(team)
    assert auto_copy_on_open(team.factory, team.ctx("ed", "editor"), OCTOBER) is None
    with team.factory() as session:
        assert session.scalars(select(MonthState)).all() == []
    food = _september_plan(team)
    assert auto_copy_on_open(team.factory, team.ctx("ed", "editor"), OCTOBER) is not None
    assert _planned(team) == {food: 55000}


def test_concurrent_page_views_copy_the_plan_once(team: WorkspaceDb) -> None:
    food, rent = _category(team, "Food"), _category(team, "Rent")
    _set(team, food, "1", month=SEPTEMBER)
    _set(team, rent, "2", month=SEPTEMBER)
    _auto(team)
    results: list[CopyResult | None] = []
    barrier = threading.Barrier(4)

    def run() -> None:
        barrier.wait()
        results.append(auto_copy_on_open(team.factory, team.ctx("ed", "editor"), OCTOBER))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [r.copied for r in results if r is not None] == [2]
    assert _planned(team) == {food: 100, rent: 200}
    assert len(_events(team, "budget_line.copied")) == 1


def test_the_automatic_copy_rechecks_after_the_lock_and_the_guard(team: WorkspaceDb) -> None:
    _september_plan(team)
    _auto(team)
    statements = _statements(
        lambda: auto_copy_on_open(team.factory, team.ctx("ed", "editor"), OCTOBER)
    )
    lock = next(i for i, s in enumerate(statements) if "FROM workspaces" in s and "FOR" in s)
    assert statements[lock].rstrip().endswith("FOR UPDATE")
    guard = next(i for i, s in enumerate(statements) if "FROM month_states" in s and i > lock)
    # The read-only decision ran before the lock; the write decides again after the lock and
    # the guard (the setting, the month's state and its lines are read anew), then copies: two
    # reads of lines for the decision and two for the copy.
    assert any("workspaces.auto_copy_plan" in s for s in statements[:lock])
    after = statements[guard + 1 :]
    setting = next(i for i, s in enumerate(after) if "workspaces.auto_copy_plan" in s)
    assert "FOR" not in after[setting]
    lines = [i for i, s in enumerate(after) if "FROM budget_lines" in s]
    assert len(lines) == 4 and setting < lines[0]
    assert any("FROM month_states" in s for s in after[setting : lines[0]])
    assert _planned(team) != {}


def test_a_busy_database_never_fails_the_page_view(
    team: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    _september_plan(team)
    _auto(team)

    def locked(*_args: Any) -> None:
        raise OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked"))

    monkeypatch.setattr(service, "run_immediate", locked)
    assert auto_copy_on_open(team.factory, team.ctx("ed", "editor"), OCTOBER) is None

    def broken(*_args: Any) -> None:
        raise OperationalError("SELECT", {}, Exception("no such table: budget_lines"))

    monkeypatch.setattr(service, "run_immediate", broken)
    with pytest.raises(OperationalError):  # anything else is a real error
        auto_copy_on_open(team.factory, team.ctx("ed", "editor"), OCTOBER)


def test_a_page_view_without_a_due_copy_does_not_write(
    team: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    _auto(team)
    monkeypatch.setattr(service, "run_immediate", lambda *_args: pytest.fail("no write"))
    assert auto_copy_on_open(team.factory, team.ctx("ed", "editor"), OCTOBER) is None


# Roles


WRITES: dict[str, Callable[[WorkspaceDb, Context, UUID], object]] = {
    "set": lambda db, ctx, cid: set_planned(
        db.factory, ctx, month=OCTOBER, category_id=cid, amount="5", locale="en"
    ),
    "remove": lambda db, ctx, cid: set_planned(
        db.factory, ctx, month=SEPTEMBER, category_id=cid, amount="0", locale="en"
    ),
    "copy": lambda db, ctx, _cid: copy_previous_plan(db.factory, ctx, OCTOBER),
}


def _target(db: WorkspaceDb) -> UUID:
    food = _category(db, "Food")
    _set(db, food, "550", month=SEPTEMBER)
    return food


@pytest.mark.parametrize("write", sorted(WRITES))
def test_viewers_cannot_write(team: WorkspaceDb, write: str) -> None:
    food = _target(team)
    before = len(_events(team))
    with pytest.raises(PermissionDenied):
        WRITES[write](team, team.ctx("vic", "viewer"), food)
    assert len(_events(team)) == before


@pytest.mark.parametrize("write", sorted(WRITES))
def test_a_stale_editor_context_is_refused_inside_the_transaction(
    team: WorkspaceDb, write: str
) -> None:
    food = _target(team)
    team.add_member("dee", "viewer")
    before = len(_events(team))
    with pytest.raises(PermissionDenied):
        WRITES[write](team, team.ctx("dee", "editor"), food)
    assert len(_events(team)) == before


@pytest.mark.parametrize("write", sorted(WRITES))
def test_the_role_is_checked_before_any_database_work(
    team: WorkspaceDb, monkeypatch: pytest.MonkeyPatch, write: str
) -> None:
    monkeypatch.setattr(service, "run_immediate", lambda *_args: pytest.fail("no write"))
    with pytest.raises(PermissionDenied) as caught:
        WRITES[write](team, team.ctx("vic", "viewer"), uuid4())
    assert caught.value.required is Role.EDITOR


@pytest.mark.parametrize("role", ["editor", "owner"])
@pytest.mark.parametrize("write", sorted(WRITES))
def test_editors_and_owners_can_write(team: WorkspaceDb, write: str, role: str) -> None:
    key = "ed" if role == "editor" else "olga"
    food = _target(team)
    WRITES[write](team, team.ctx(key, role), food)
    assert _events(team)[-1].actor_user_id == team.users[key]


# Closed months are read-only for planned values


@pytest.mark.parametrize("write", sorted(WRITES))
@pytest.mark.parametrize("key", ["ed", "olga"])
def test_closed_months_refuse_every_plan_write(team: WorkspaceDb, write: str, key: str) -> None:
    food = _target(team)
    _close(team, SEPTEMBER)
    _close(team, OCTOBER)
    lines, events = _lines(team), len(_events(team))
    with pytest.raises(MonthClosed):
        WRITES[write](team, team.ctx(key, "owner" if key == "olga" else "editor"), food)
    assert [line.id for line in _lines(team)] == [line.id for line in lines]
    assert len(_events(team)) == events
    reopen_month(team.factory, team.ctx("olga", "owner"), OCTOBER)
    assert copy_previous_plan(team.factory, team.ctx("ed", "editor"), OCTOBER) is not None


def test_a_closed_source_month_can_still_be_copied_from(team: WorkspaceDb) -> None:
    food = _target(team)
    _close(team, SEPTEMBER)
    copy_previous_plan(team.factory, team.ctx("ed", "editor"), OCTOBER)
    assert _planned(team) == {food: 55000}


def _statements(operation: Callable[[], Any]) -> list[str]:
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


@pytest.mark.parametrize("write", sorted(WRITES))
def test_writes_lock_exclusively_then_guard_before_reading_lines(
    team: WorkspaceDb, write: str
) -> None:
    food = _target(team)
    statements = _statements(lambda: WRITES[write](team, team.ctx("ed", "editor"), food))
    lock = next(i for i, s in enumerate(statements) if "FROM workspaces" in s and "FOR" in s)
    assert statements[lock].rstrip().endswith("FOR UPDATE")
    guard = next(i for i, s in enumerate(statements) if "FROM month_states" in s)
    reads = [i for i, s in enumerate(statements) if "FROM budget_lines" in s]
    assert lock < guard < min(reads)


# Seams: category usage and the base-currency lock


def test_category_usage_counts_lines_and_their_months(team: WorkspaceDb) -> None:
    food, other = _category(team, "Food"), _category(team, "Other")
    _set(team, food, "1", month=SEPTEMBER)
    _set(team, food, "2")
    with team.factory() as session:
        usage = category_usage(session, team.ctx("vic", "viewer"))
    assert usage == {
        food: CategoryUsage(references=2, active_months=frozenset({SEPTEMBER, OCTOBER}))
    }
    assert other not in usage


def test_a_planned_value_blocks_kind_change_and_delete(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    _set(team, food, "550")
    shown = get_category(team.factory, team.ctx("vic", "viewer"), food)
    assert (shown.in_use, shown.active_transactions) == (True, 0)
    assert delete_category(team.factory, team.ctx("ed", "editor"), food) == "archived"


def test_a_treatment_change_lists_closed_months_with_planned_values(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    _set(team, food, "1", month=AUGUST)
    _set(team, food, "2", month=SEPTEMBER)
    _close(team, AUGUST)
    shown = get_category(team.factory, team.ctx("vic", "viewer"), food)
    assert shown.closed_months_affected == (AUGUST,)


def test_workspace_has_budget_lines_counts_existing_lines_only(team: WorkspaceDb) -> None:
    def has() -> bool:
        with team.factory() as session:
            return workspace_has_budget_lines(session, team.ctx("vic", "viewer"))

    assert not has()
    food = _category(team, "Food")
    _set(team, food, "550")
    assert has()
    _set(team, food, "0")
    assert not has()
