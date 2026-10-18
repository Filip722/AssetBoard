"""Transactions service (F2.2 part A): entry rules, undo, roles, the closed-month guard, audit,
autocomplete and the seams used by categories and workspace settings."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import ORMExecuteState, Session
from sqlalchemy.sql import ClauseElement

from assetboard.clock import FixedClock
from assetboard.db.models.identity import AuditEvent, Workspace
from assetboard.db.models.ledger import Transaction
from assetboard.db.repositories import NotFound
from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.services import transactions as service
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
from assetboard.services.transactions import (
    InvalidTransaction,
    Suggestion,
    TransactionView,
    UndoExpired,
    category_usage,
    create_transaction,
    delete_transaction,
    get_transaction,
    list_month,
    suggest_descriptions,
    undo_delete,
    update_transaction,
    workspace_has_transactions,
)
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

SEPTEMBER, OCTOBER, NOVEMBER = YearMonth(2026, 9), YearMonth(2026, 10), YearMonth(2026, 11)
START = datetime(2026, 10, 14, 12, tzinfo=UTC)


class SteppingClock(FixedClock):
    """A clock tests move by hand; each ``now()`` also advances one microsecond so rows and
    audit events sort in the order they were made."""

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


def _clock(db: WorkspaceDb) -> SteppingClock:
    assert isinstance(db.clock, SteppingClock)
    return db.clock


def _category(db: WorkspaceDb, name: str, kind: str = "expense") -> UUID:
    return create_category(
        db.factory, db.ctx("ed", "editor"), name=name, kind=kind, treatment="regular",
        color="#16a34a",
    ).id  # fmt: skip


def _add(
    db: WorkspaceDb,
    category_id: UUID | None,
    *,
    day: str = "7",
    amount: str = "12,50",
    description: str = "Lidl",
    direction: str = "expense",
    month: YearMonth = OCTOBER,
    ctx: Context | None = None,
    locale: str = "pl",
) -> TransactionView:
    return create_transaction(
        db.factory, ctx or db.ctx("ed", "editor"), month=month, direction=direction, day=day,
        amount=amount, description=description, category_id=category_id, locale=locale,
    )  # fmt: skip


def _edit(
    db: WorkspaceDb, view: TransactionView, ctx: Context | None = None, **changes: Any
) -> TransactionView:
    values: dict[str, Any] = {
        "month": view.month,
        "day": str(view.date.day),
        "amount": str(view.amount.to_decimal()),
        "description": view.description,
        "category_id": view.category_id,
        "locale": "en",
    }
    values |= changes
    return update_transaction(db.factory, ctx or db.ctx("ed", "editor"), view.id, **values)


def _events(db: WorkspaceDb, prefix: str = "transaction.") -> list[AuditEvent]:
    with db.factory() as session:
        statement = select(AuditEvent).where(AuditEvent.action.startswith(prefix))
        return list(session.scalars(statement.order_by(AuditEvent.ts, AuditEvent.id)))


def _invalid(operation: Callable[[], Any]) -> dict[str, str]:
    with pytest.raises(InvalidTransaction) as caught:
        operation()
    return caught.value.errors


# Create: entry rules


def test_create_stores_minor_units_in_the_base_currency_and_audits(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    view = _add(team, food, day="7", amount="1 234,56", description="  Weekly   shop ")
    assert (view.date, view.amount, view.description) == (
        date(2026, 10, 7),
        Money(123456, "EUR"),
        "Weekly shop",
    )
    assert (view.direction, view.category_name, view.source) == ("expense", "Food", "manual")
    with team.factory() as session:
        row = session.get(Transaction, view.id)
        assert row is not None
        assert (row.amount_minor, row.currency, row.description_key) == (
            123456,
            "EUR",
            "weekly shop",
        )
        assert row.created_by_user_id == team.users["ed"]
    event_ = _events(team)[-1]
    assert (event_.action, event_.entity_type, event_.entity_id) == (
        "transaction.created",
        "transaction",
        str(view.id),
    )
    assert event_.data == {
        "date": "2026-10-07",
        "direction": "expense",
        "category": "Food",
        "amount": "1234.56",
        "currency": "EUR",
    }


@pytest.mark.parametrize(
    ("amount", "locale", "minor"),
    [
        ("12,50", "pl", 1250),
        ("1 234,56", "pl", 123456),
        ("1 234,56", "pl", 123456),
        ("1,234.56", "en", 123456),
        ("12.5", "en", 1250),
        ("12,5", "en", 1250),
        ("7", "en", 700),
    ],
)
def test_amounts_are_parsed_in_the_locale_format(
    team: WorkspaceDb, amount: str, locale: str, minor: int
) -> None:
    food = _category(team, "Food")
    assert _add(team, food, amount=amount, locale=locale).amount == Money(minor, "EUR")


@pytest.mark.parametrize(
    ("amount", "code"),
    [
        ("0", "amount_not_positive"),
        ("0,00", "amount_not_positive"),
        ("-5", "amount_not_positive"),
        ("12,345", "too_many_decimals"),  # pl: 12.345 has three decimals, not grouping
        ("10000000000000", "amount_too_large"),
        ("abc", "invalid_amount"),
        ("", "invalid_amount"),
        ("1e5", "invalid_amount"),
    ],
)
def test_invalid_amounts_are_refused(team: WorkspaceDb, amount: str, code: str) -> None:
    food = _category(team, "Food")
    assert _invalid(lambda: _add(team, food, amount=amount)) == {"amount": code}
    assert _events(team) == []


def test_day_numbers_are_read_within_the_viewed_month(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    assert _add(team, food, day="30", month=NOVEMBER).date == date(2026, 11, 30)
    assert _invalid(lambda: _add(team, food, day="31", month=NOVEMBER)) == {
        "day": "day_not_in_month"
    }
    assert _invalid(lambda: _add(team, food, day="29", month=YearMonth(2026, 2))) == {
        "day": "day_not_in_month"
    }
    assert _add(team, food, day="29", month=YearMonth(2028, 2)).date == date(2028, 2, 29)


def test_a_full_date_may_be_in_another_month(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    view = _add(team, food, day="30.09.2026", month=OCTOBER)
    assert (view.date, view.month) == (date(2026, 9, 30), SEPTEMBER)
    assert [
        v.id for v in list_month(team.factory, team.ctx("vic", "viewer"), SEPTEMBER).expense
    ] == [view.id]
    assert list_month(team.factory, team.ctx("vic", "viewer"), OCTOBER).expense == ()


def test_every_field_error_is_reported_at_once(team: WorkspaceDb) -> None:
    errors = _invalid(lambda: _add(team, None, day="32", amount="x", description="a\x00b"))
    assert errors == {
        "day": "invalid_date",
        "amount": "invalid_amount",
        "description": "invalid_description",
        "category": "category_required",
    }


def test_the_direction_must_be_known(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    assert _invalid(lambda: _add(team, food, direction="transfer")) == {
        "direction": "invalid_direction"
    }


def test_the_direction_must_match_the_category_kind(team: WorkspaceDb) -> None:
    food, salary = _category(team, "Food"), _category(team, "Salary", "income")
    assert _invalid(lambda: _add(team, salary, direction="expense")) == {
        "category": "category_kind_mismatch"
    }
    assert _invalid(lambda: _add(team, food, direction="income")) == {
        "category": "category_kind_mismatch"
    }
    income = _add(team, salary, direction="income", amount="3400")
    assert income.direction == "income"
    # An edit cannot move a row to a category of the other kind either.
    assert _invalid(lambda: _edit(team, income, category_id=food)) == {
        "category": "category_kind_mismatch"
    }
    assert len(_events(team)) == 1


def test_archived_categories_take_no_new_transactions_but_keep_old_ones(
    team: WorkspaceDb,
) -> None:
    food, other = _category(team, "Food"), _category(team, "Other")
    view = _add(team, food)
    archive_category(team.factory, team.ctx("ed", "editor"), food)
    assert _invalid(lambda: _add(team, food)) == {"category": "category_archived"}
    kept = _edit(team, view, amount="13.00")  # the archived category stays on the row
    assert (kept.category_id, kept.category_archived, kept.amount) == (
        food,
        True,
        Money(1300, "EUR"),
    )
    moved = _edit(team, kept, category_id=other)
    assert moved.category_id == other
    assert _invalid(lambda: _edit(team, moved, category_id=food)) == {
        "category": "category_archived"
    }


def test_another_workspaces_or_an_unknown_category_is_invalid(team: WorkspaceDb) -> None:
    other = team.add_workspace("Other")
    team.add_member("ed", "editor", other)
    foreign = create_category(
        team.factory, team.ctx("ed", "editor", other), name="Food", kind="expense",
        treatment="regular", color="#16a34a",
    )  # fmt: skip
    for category_id in (foreign.id, uuid4()):
        assert _invalid(lambda cid=category_id: _add(team, cid)) == {"category": "invalid_category"}


def test_the_currency_is_read_from_the_workspace_not_the_context(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    with team.factory.begin() as session:  # changed after the request's Context was built
        workspace = session.get(Workspace, team.workspace_id)
        assert workspace is not None
        workspace.base_currency = "JPY"
    stale = team.ctx("ed", "editor")
    assert stale.workspace.base_currency == "EUR"
    assert _add(team, food, amount="1500", ctx=stale).amount == Money(1500, "JPY")
    # The decimals are checked against the stored currency too.
    assert _invalid(lambda: _add(team, food, amount="15,50", ctx=stale)) == {
        "amount": "too_many_decimals"
    }


# Lists and reads


def test_list_month_splits_directions_orders_by_date_then_entry_and_skips_deleted(
    team: WorkspaceDb,
) -> None:
    food, salary = _category(team, "Food"), _category(team, "Salary", "income")
    second = _add(team, food, day="9", description="second")
    first = _add(team, food, day="2", description="first")
    third = _add(team, food, day="9", description="third")  # same day: entry order
    gone = _add(team, food, day="3", description="gone")
    _add(team, food, day="2026-11-01")  # another month
    pay = _add(team, salary, direction="income", day="10", amount="3400")
    delete_transaction(team.factory, team.ctx("ed", "editor"), gone.id)
    listed = list_month(team.factory, team.ctx("vic", "viewer"), OCTOBER)
    assert [v.id for v in listed.expense] == [first.id, second.id, third.id]
    assert [v.id for v in listed.income] == [pay.id]
    assert listed.month == OCTOBER


def test_get_transaction_hides_deleted_and_foreign_rows(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    view = _add(team, food)
    assert get_transaction(team.factory, team.ctx("vic", "viewer"), view.id) == view
    other = team.add_workspace("Other")
    team.add_member("ed", "editor", other)
    with pytest.raises(NotFound):
        get_transaction(team.factory, team.ctx("ed", "editor", other), view.id)
    delete_transaction(team.factory, team.ctx("ed", "editor"), view.id)
    with pytest.raises(NotFound):
        get_transaction(team.factory, team.ctx("vic", "viewer"), view.id)


# Update


def test_update_changes_fields_and_audits_previous_values(team: WorkspaceDb) -> None:
    food, eating = _category(team, "Food"), _category(team, "Eating out")
    view = _add(team, food, description="Lidl")
    changed = _edit(team, view, day="12", amount="20.00", description="Pizza", category_id=eating)
    assert (changed.date, changed.amount, changed.description, changed.category_name) == (
        date(2026, 10, 12),
        Money(2000, "EUR"),
        "Pizza",
        "Eating out",
    )
    assert _events(team)[-1].data == {
        "fields": "amount,category,date,description",
        "date": "2026-10-12",
        "previous_date": "2026-10-07",
        "direction": "expense",
        "category": "Eating out",
        "amount": "20.00",
        "previous_amount": "12.50",
        "currency": "EUR",
    }
    assert "Pizza" not in str(_events(team)[-1].data)  # never the description
    with team.factory() as session:
        row = session.get(Transaction, view.id)
        assert row is not None and row.description_key == "pizza"


def test_update_without_changes_writes_no_event(team: WorkspaceDb) -> None:
    view = _add(team, _category(team, "Food"))
    assert _edit(team, view) == view
    assert len(_events(team)) == 1


def test_a_deleted_or_foreign_transaction_cannot_be_edited(team: WorkspaceDb) -> None:
    view = _add(team, _category(team, "Food"))
    other = team.add_workspace("Other")
    team.add_member("ed", "editor", other)
    with pytest.raises(NotFound):
        _edit(team, view, ctx=team.ctx("ed", "editor", other), amount="1")
    delete_transaction(team.factory, team.ctx("ed", "editor"), view.id)
    with pytest.raises(NotFound):
        _edit(team, view, amount="1")


# Soft delete and undo


def test_delete_soft_deletes_and_undo_within_ten_seconds_restores(team: WorkspaceDb) -> None:
    view = _add(team, _category(team, "Food"))
    deleted = delete_transaction(team.factory, team.ctx("ed", "editor"), view.id)
    with team.factory() as session:
        row = session.get(Transaction, view.id)
        assert row is not None and row.deleted_at is not None
        assert deleted.undo_until == row.deleted_at + timedelta(seconds=10)
    assert deleted.month == OCTOBER
    _clock(team).at = deleted.undo_until - timedelta(microseconds=1)  # now() steps 1 µs: exact
    restored = undo_delete(team.factory, team.ctx("olga", "owner"), view.id)  # any editor/owner
    assert restored == view
    assert [v.id for v in list_month(team.factory, team.ctx("vic", "viewer"), OCTOBER).expense] == [
        view.id
    ]
    assert [e.action for e in _events(team)] == [
        "transaction.created",
        "transaction.deleted",
        "transaction.restored",
    ]


def test_undo_after_the_window_is_refused(team: WorkspaceDb) -> None:
    view = _add(team, _category(team, "Food"))
    deleted = delete_transaction(team.factory, team.ctx("ed", "editor"), view.id)
    _clock(team).at = deleted.undo_until  # the next now() is 1 µs past the window
    with pytest.raises(UndoExpired):
        undo_delete(team.factory, team.ctx("ed", "editor"), view.id)
    with pytest.raises(NotFound):
        get_transaction(team.factory, team.ctx("ed", "editor"), view.id)
    assert _events(team)[-1].action == "transaction.deleted"


def test_delete_twice_and_undo_of_a_live_row_change_nothing(team: WorkspaceDb) -> None:
    view = _add(team, _category(team, "Food"))
    assert undo_delete(team.factory, team.ctx("ed", "editor"), view.id) == view
    first = delete_transaction(team.factory, team.ctx("ed", "editor"), view.id)
    again = delete_transaction(team.factory, team.ctx("ed", "editor"), view.id)
    assert again == first  # the original deletion time (and undo window) stays
    assert [e.action for e in _events(team)] == ["transaction.created", "transaction.deleted"]


def test_a_foreign_transaction_cannot_be_deleted_or_restored(team: WorkspaceDb) -> None:
    view = _add(team, _category(team, "Food"))
    other = team.add_workspace("Other")
    team.add_member("ed", "editor", other)
    foreign = team.ctx("ed", "editor", other)
    with pytest.raises(NotFound):
        delete_transaction(team.factory, foreign, view.id)
    delete_transaction(team.factory, team.ctx("ed", "editor"), view.id)
    with pytest.raises(NotFound):
        undo_delete(team.factory, foreign, view.id)


def test_undo_keeps_a_category_archived_meanwhile(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    view = _add(team, food)
    delete_transaction(team.factory, team.ctx("ed", "editor"), view.id)
    assert delete_category(team.factory, team.ctx("ed", "editor"), food) == "archived"
    restored = undo_delete(team.factory, team.ctx("ed", "editor"), view.id)
    assert (restored.category_id, restored.category_archived) == (food, True)


# Roles


WRITES: dict[str, Callable[[WorkspaceDb, Context, UUID, UUID], Any]] = {
    "create": lambda db, ctx, cid, _tid: _add(db, cid, ctx=ctx),
    "update": lambda db, ctx, cid, tid: update_transaction(
        db.factory,
        ctx,
        tid,
        month=OCTOBER,
        day="8",
        amount="1",
        description="x",
        category_id=cid,
        locale="en",
    ),  # fmt: skip
    "delete": lambda db, ctx, _cid, tid: delete_transaction(db.factory, ctx, tid),
    "undo": lambda db, ctx, _cid, tid: undo_delete(db.factory, ctx, tid),
}


def _target(db: WorkspaceDb, write: str) -> tuple[UUID, UUID]:
    food = _category(db, "Food")
    view = _add(db, food)
    if write == "undo":
        delete_transaction(db.factory, db.ctx("ed", "editor"), view.id)
    return food, view.id


@pytest.mark.parametrize("write", sorted(WRITES))
def test_viewers_cannot_write(team: WorkspaceDb, write: str) -> None:
    food, target = _target(team, write)
    before = len(_events(team))
    with pytest.raises(PermissionDenied):
        WRITES[write](team, team.ctx("vic", "viewer"), food, target)
    assert len(_events(team)) == before


@pytest.mark.parametrize("write", sorted(WRITES))
def test_a_stale_editor_context_is_refused_inside_the_transaction(
    team: WorkspaceDb, write: str
) -> None:
    food, target = _target(team, write)
    team.add_member("dee", "viewer")  # demoted: the Context still says editor
    before = len(_events(team))
    with pytest.raises(PermissionDenied):
        WRITES[write](team, team.ctx("dee", "editor"), food, target)
    assert len(_events(team)) == before


@pytest.mark.parametrize("write", sorted(WRITES))
def test_the_role_is_checked_before_any_database_work(
    team: WorkspaceDb, monkeypatch: pytest.MonkeyPatch, write: str
) -> None:
    monkeypatch.setattr(service, "run_immediate", lambda *_args: pytest.fail("no write"))
    with pytest.raises(PermissionDenied) as caught:
        WRITES[write](team, team.ctx("vic", "viewer"), uuid4(), uuid4())
    assert caught.value.required is Role.EDITOR


@pytest.mark.parametrize("role", ["editor", "owner"])
@pytest.mark.parametrize("write", sorted(WRITES))
def test_editors_and_owners_can_write(team: WorkspaceDb, write: str, role: str) -> None:
    key = "ed" if role == "editor" else "olga"
    food, target = _target(team, write)
    WRITES[write](team, team.ctx(key, role), food, target)
    assert _events(team)[-1].actor_user_id == team.users[key]


def test_viewers_can_read(team: WorkspaceDb) -> None:
    view = _add(team, _category(team, "Food"))
    viewer = team.ctx("vic", "viewer")
    assert list_month(team.factory, viewer, OCTOBER).expense == (view,)
    assert suggest_descriptions(team.factory, viewer, direction="expense", prefix="l") == [
        Suggestion("Lidl", view.category_id)
    ]


# The closed-month guard (F2.5 obligations)


def _close(db: WorkspaceDb, month: YearMonth) -> None:
    close_month(db.factory, db.ctx("olga", "owner"), month)


def test_creating_in_a_closed_month_is_refused_for_everyone(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    _close(team, OCTOBER)
    for key, role in (("ed", "editor"), ("olga", "owner")):
        with pytest.raises(MonthClosed):
            _add(team, food, ctx=team.ctx(key, role))
    with pytest.raises(MonthClosed):  # a full date into a closed month from an open page
        _add(team, food, day="2026-10-01", month=NOVEMBER)
    assert _events(team) == []


def test_editing_checks_the_old_and_the_new_month(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    in_september = _add(team, food, day="2026-09-15")
    in_october = _add(team, food)
    _close(team, SEPTEMBER)
    with pytest.raises(MonthClosed) as caught:  # out of a closed month, even into an open one
        _edit(team, in_september, month=OCTOBER, day="20")
    assert caught.value.month == SEPTEMBER
    with pytest.raises(MonthClosed) as caught:  # into a closed month
        _edit(team, in_october, day="2026-09-20")
    assert caught.value.month == SEPTEMBER
    with pytest.raises(MonthClosed):  # within a closed month
        _edit(team, in_september, amount="1.00")
    assert len(_events(team)) == 2
    reopen_month(team.factory, team.ctx("olga", "owner"), SEPTEMBER)
    assert _edit(team, in_october, day="2026-09-20").month == SEPTEMBER


def test_delete_and_undo_check_the_stored_month(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    view = _add(team, food)
    _close(team, OCTOBER)
    with pytest.raises(MonthClosed):
        delete_transaction(team.factory, team.ctx("olga", "owner"), view.id)
    reopen_month(team.factory, team.ctx("olga", "owner"), OCTOBER)
    delete_transaction(team.factory, team.ctx("ed", "editor"), view.id)
    _close(team, OCTOBER)  # closed within the undo window
    with pytest.raises(MonthClosed):
        undo_delete(team.factory, team.ctx("ed", "editor"), view.id)
    with pytest.raises(NotFound):  # the row stays deleted
        get_transaction(team.factory, team.ctx("ed", "editor"), view.id)


def _statements(operation: Callable[[], Any]) -> list[str]:
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


def _assert_lock_then_read_then_guard(statements: list[str]) -> None:
    lock = next(i for i, s in enumerate(statements) if "FROM workspaces" in s and "FOR" in s)
    assert statements[lock].rstrip().endswith("FOR SHARE")
    reads = [i for i, s in enumerate(statements) if "FROM transactions" in s]
    guards = [i for i, s in enumerate(statements) if "FROM month_states" in s]
    assert reads and guards
    # Nothing touches the transactions before the lock; the stored row is read before the
    # guard checks its month.
    assert lock < min(reads) < min(guards)
    assert not any("FROM transactions" in s for s in statements[:lock])


@pytest.mark.parametrize("write", ["update", "delete", "undo"])
def test_writes_on_stored_rows_lock_before_reading_them(team: WorkspaceDb, write: str) -> None:
    food, target = _target(team, write)
    statements = _statements(lambda: WRITES[write](team, team.ctx("ed", "editor"), food, target))
    _assert_lock_then_read_then_guard(statements)
    # The stored row itself is read with a row lock, so writes to it are serialised.
    first_read = next(s for s in statements if "FROM transactions" in s)
    assert first_read.rstrip().endswith("FOR UPDATE")


def test_the_update_guards_the_old_and_the_new_month_after_reading_the_row(
    team: WorkspaceDb,
) -> None:
    food = _category(team, "Food")
    view = _add(team, food)
    statements = _statements(lambda: _edit(team, view, day="2026-11-03"))
    _assert_lock_then_read_then_guard(statements)
    guards = [s for s in statements if "FROM month_states" in s]
    assert len(guards) == 2  # October (stored) and November (new)


def test_create_locks_before_the_guard_and_reads_the_currency_after(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    statements = _statements(lambda: _add(team, food))
    lock = next(i for i, s in enumerate(statements) if "FROM workspaces" in s and "FOR" in s)
    guard = next(i for i, s in enumerate(statements) if "FROM month_states" in s)
    currency = next(
        i for i, s in enumerate(statements) if "workspaces.base_currency" in s and "FOR" not in s
    )
    assert lock < guard < currency


# Autocomplete


def test_suggestions_match_a_prefix_in_any_case_and_skip_deleted_rows(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    _add(team, food, description="Żabka")
    _add(team, food, description="żabka", day="8")
    _add(team, food, description="żabka", day="9")
    _add(team, food, description="Zara")
    gone = _add(team, food, description="Żółw")
    delete_transaction(team.factory, team.ctx("ed", "editor"), gone.id)
    viewer = team.ctx("vic", "viewer")
    found = suggest_descriptions(team.factory, viewer, direction="expense", prefix="ŻA")
    assert [s.description for s in found] == ["żabka"]  # the most used spelling
    assert suggest_descriptions(team.factory, viewer, direction="expense", prefix="żó") == []
    assert suggest_descriptions(team.factory, viewer, direction="expense", prefix="  ") == []


def test_suggestions_are_per_direction_and_most_used_first(team: WorkspaceDb) -> None:
    food, bonus = _category(team, "Food"), _category(team, "Bonus", "income")
    _add(team, food, description="Bakery")
    for _ in range(2):
        _add(team, food, description="Bank fee")
    _add(team, bonus, direction="income", description="Bank interest")
    viewer = team.ctx("vic", "viewer")
    expense = suggest_descriptions(team.factory, viewer, direction="expense", prefix="ba")
    assert [s.description for s in expense] == ["Bank fee", "Bakery"]
    income = suggest_descriptions(team.factory, viewer, direction="income", prefix="ba")
    assert income == [Suggestion("Bank interest", bonus)]
    assert suggest_descriptions(team.factory, viewer, direction="expense", prefix="b", limit=1) == [
        Suggestion("Bank fee", food)
    ]


def test_the_suggested_category_is_the_most_frequent_active_one(team: WorkspaceDb) -> None:
    food, eating, gifts = (
        _category(team, "Food"),
        _category(team, "Eating out"),
        _category(team, "Gifts"),
    )
    for _ in range(3):
        _add(team, gifts, description="Lidl")
    for _ in range(2):
        _add(team, food, description="Lidl", day="1")
    for _ in range(2):
        _add(team, eating, description="Lidl", day="20")
    viewer = team.ctx("vic", "viewer")

    def suggested() -> UUID | None:
        return suggest_descriptions(team.factory, viewer, direction="expense", prefix="li")[
            0
        ].category_id

    assert suggested() == gifts
    archive_category(team.factory, team.ctx("ed", "editor"), gifts)
    assert suggested() == eating  # a tie with Food (2 each): the most recently dated wins
    archive_category(team.factory, team.ctx("ed", "editor"), eating)
    archive_category(team.factory, team.ctx("ed", "editor"), food)
    assert suggest_descriptions(team.factory, viewer, direction="expense", prefix="li") == [
        Suggestion("Lidl", None)
    ]


def test_keys_are_ranked_over_all_rows_not_a_cut_of_grouped_rows(team: WorkspaceDb) -> None:
    """A key used often with many spellings and categories still ranks first.

    "Shop" is used 4 times, but as 4 different (spelling, category) pairs used once each;
    "Shoes" twice as one pair. Ranking a capped list of grouped pairs (the first version)
    would have put "Shoes" first; keys are now ranked in SQL over every row.
    """
    a, b = _category(team, "A"), _category(team, "B")
    for spelling, category in (("Shop", a), ("SHOP", a), ("Shop", b), ("shop", b)):
        _add(team, category, description=spelling, day="1")
    for _ in range(2):
        _add(team, a, description="Shoes", day="20")
    viewer = team.ctx("vic", "viewer")
    found = suggest_descriptions(team.factory, viewer, direction="expense", prefix="sho", limit=1)
    assert found == [Suggestion("Shop", a)]  # spelling: 2 uses; category: tie, A first in order
    found = suggest_descriptions(team.factory, viewer, direction="expense", prefix="sho")
    assert [s.description for s in found] == ["Shop", "Shoes"]


def test_like_wildcards_in_the_prefix_are_literal(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    _add(team, food, description="50% off")
    _add(team, food, description="500 bar")
    _add(team, food, description="5_x")
    viewer = team.ctx("vic", "viewer")
    found = suggest_descriptions(team.factory, viewer, direction="expense", prefix="50%")
    assert [s.description for s in found] == ["50% off"]
    found = suggest_descriptions(team.factory, viewer, direction="expense", prefix="5_")
    assert [s.description for s in found] == ["5_x"]


def test_suggestions_never_cross_workspaces(team: WorkspaceDb) -> None:
    _add(team, _category(team, "Food"), description="Lidl")
    other = team.add_workspace("Other")
    team.add_member("ed", "editor", other)
    foreign = team.ctx("ed", "editor", other)
    assert suggest_descriptions(team.factory, foreign, direction="expense", prefix="l") == []


# Seams: category usage and the base-currency lock


def test_category_usage_counts_deleted_references_and_active_months(team: WorkspaceDb) -> None:
    food, other = _category(team, "Food"), _category(team, "Other")
    _add(team, food, day="2026-09-03")
    _add(team, food)
    gone = _add(team, food, day="2026-08-01")
    delete_transaction(team.factory, team.ctx("ed", "editor"), gone.id)
    with team.factory() as session:
        usage = category_usage(session, team.ctx("vic", "viewer"))
    assert usage == {
        food: CategoryUsage(
            references=3, active_transactions=2, active_months=frozenset({SEPTEMBER, OCTOBER})
        )
    }
    assert other not in usage


def test_a_soft_deleted_transaction_alone_blocks_kind_change_and_delete(
    team: WorkspaceDb,
) -> None:
    food = _category(team, "Food")
    view = _add(team, food)
    delete_transaction(team.factory, team.ctx("ed", "editor"), view.id)
    shown = get_category(team.factory, team.ctx("vic", "viewer"), food)
    assert (shown.in_use, shown.active_transactions) == (True, 0)
    assert delete_category(team.factory, team.ctx("ed", "editor"), food) == "archived"


def test_the_category_lists_closed_months_a_treatment_change_affects(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    for day in ("2026-08-03", "2026-09-04", "2026-09-05", "2026-10-06"):
        _add(team, food, day=day)
    gone = _add(team, food, day="2026-07-01")
    delete_transaction(team.factory, team.ctx("ed", "editor"), gone.id)
    for month in (YearMonth(2026, 7), YearMonth(2026, 8), SEPTEMBER):
        _close(team, month)
    shown = get_category(team.factory, team.ctx("vic", "viewer"), food)
    # July is closed but holds only a deleted row; October is open.
    assert shown.closed_months_affected == (YearMonth(2026, 8), SEPTEMBER)
    update_category(
        team.factory, team.ctx("ed", "editor"), food, name="Food", kind="expense",
        treatment="savings", color="#16a34a",
    )  # fmt: skip
    data = _events(team, "category.updated")[-1].data
    assert (data["affected_transactions"], data["closed_months_affected"]) == (4, 2)


def test_workspace_has_transactions_counts_deleted_rows(team: WorkspaceDb) -> None:
    def has() -> bool:
        with team.factory() as session:
            return workspace_has_transactions(session, team.ctx("vic", "viewer"))

    assert not has()
    view = _add(team, _category(team, "Food"))
    delete_transaction(team.factory, team.ctx("ed", "editor"), view.id)
    assert has()
