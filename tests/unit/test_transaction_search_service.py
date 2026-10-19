"""Transaction search, CSV export and bulk recategorize through the service API (F2.6)."""

from __future__ import annotations

import csv
import io
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
from assetboard.db.models.identity import AuditEvent, Membership
from assetboard.db.models.ledger import Transaction
from assetboard.domain.ledger_csv import CsvOptions
from assetboard.domain.months import YearMonth
from assetboard.domain.transaction_search import SortOrder, TransactionFilter
from assetboard.services import transaction_search as service
from assetboard.services.categories import archive_category, create_category
from assetboard.services.context import Context, PermissionDenied, Role
from assetboard.services.months import close_month
from assetboard.services.transaction_search import (
    MAX_SELECTED_IDS,
    FilterInput,
    InvalidFilter,
    InvalidTarget,
    KindMismatch,
    MonthsClosed,
    NothingSelected,
    Selection,
    SelectionChanged,
    TooManySelected,
    TooManyTicked,
    category_options,
    export_transactions_csv,
    parse_filter,
    preview_recategorize,
    recategorize_transactions,
    search_transactions,
)
from assetboard.services.transactions import (
    create_transaction,
    delete_transaction,
    update_transaction,
)
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

START = datetime(2026, 10, 14, 12, tzinfo=UTC)


class SteppingClock(FixedClock):
    """Each ``now()`` advances one microsecond, so rows sort in the order they were made."""

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


def _ed(db: WorkspaceDb) -> Context:
    return db.ctx("ed", "editor")


def _category(db: WorkspaceDb, name: str, kind: str = "expense") -> UUID:
    return create_category(
        db.factory, _ed(db), name=name, kind=kind, treatment="regular", color="#16a34a"
    ).id


def _add(
    db: WorkspaceDb,
    category_id: UUID,
    day: str,
    amount: str = "12.50",
    description: str = "Lidl",
    direction: str = "expense",
) -> UUID:
    parsed = date.fromisoformat(day)
    return create_transaction(
        db.factory, _ed(db), month=YearMonth.from_date(parsed), direction=direction,
        day=day, amount=amount, description=description, category_id=category_id, locale="en",
    ).id  # fmt: skip


def _where(db: WorkspaceDb, locale: str = "en", **fields: Any) -> TransactionFilter:
    return parse_filter(FilterInput(**fields), locale=locale, currency="EUR")


def _search(
    db: WorkspaceDb,
    where: TransactionFilter | None = None,
    *,
    sort: SortOrder = SortOrder.DATE_DESC,
    page: int = 1,
    ctx: Context | None = None,
) -> service.SearchResult:
    return search_transactions(
        db.factory, ctx or db.ctx("vic", "viewer"), where or TransactionFilter(),
        sort=sort, page=page,
    )  # fmt: skip


def _descriptions(result: service.SearchResult) -> list[str]:
    return [row.description for row in result.rows]


def _errors(**fields: Any) -> dict[str, str]:
    with pytest.raises(InvalidFilter) as caught:
        parse_filter(FilterInput(**fields), locale=fields.pop("locale", "en"), currency="EUR")
    return caught.value.errors


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


def _events(db: WorkspaceDb, action: str) -> list[AuditEvent]:
    with db.factory() as session:
        return list(session.scalars(select(AuditEvent).where(AuditEvent.action == action)))


def _categories_of(db: WorkspaceDb) -> dict[UUID, UUID]:
    with db.factory() as session:
        return {row.id: row.category_id for row in session.scalars(select(Transaction))}


@pytest.fixture
def ledger(team: WorkspaceDb) -> dict[str, UUID]:
    """Rows in three months, both directions, an archived category and a deleted row."""
    ids: dict[str, UUID] = {}
    food, car = _category(team, "Food"), _category(team, "Car")
    salary = _category(team, "Salary", "income")
    ids |= {"food": food, "car": car, "salary": salary}
    ids["aug"] = _add(team, food, "2026-08-31", "5.00", "Bakery")
    ids["sep"] = _add(team, food, "2026-09-01", "1234.56", "Żabka weekly")
    ids["sep_car"] = _add(team, car, "2026-09-15", "80", "Fuel 100%")
    ids["oct"] = _add(team, food, "2026-10-01", "12.50", "lidl")
    ids["pay"] = _add(team, salary, "2026-09-30", "3400", "September pay", "income")
    gone = _add(team, food, "2026-09-02", "9.99", "Deleted entry")
    delete_transaction(team.factory, _ed(team), gone)
    archive_category(team.factory, _ed(team), car)
    return ids


# Search: across months, deleted rows hidden, filters


def test_the_list_spans_every_month_and_hides_deleted_rows(
    team: WorkspaceDb, ledger: dict[str, UUID]
) -> None:
    result = _search(team)
    assert _descriptions(result) == [
        "lidl",
        "September pay",
        "Fuel 100%",
        "Żabka weekly",
        "Bakery",
    ]
    assert result.window.total == 5
    fuel = next(row for row in result.rows if row.id == ledger["sep_car"])
    assert (fuel.category_name, fuel.category_archived, fuel.amount.amount_minor) == (
        "Car",
        True,
        8000,
    )
    # Every category is offered for the filter, archived ones included.
    names = [option.name for option in result.categories]
    assert set(names) == {"Food", "Car", "Salary"}


def test_viewers_can_search_and_list_categories(team: WorkspaceDb, ledger: dict) -> None:
    viewer = team.ctx("vic", "viewer")
    assert _search(team, ctx=viewer).window.total == 5
    assert len(category_options(team.factory, viewer)) == 3


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"date_from": "2026-09-01", "date_to": "2026-09-30"}, {"sep", "sep_car", "pay"}),
        ({"date_from": "2026-09-30"}, {"pay", "oct"}),  # inclusive bounds
        ({"date_to": "2026-08-31"}, {"aug"}),
        ({"date_from": "01.10.2026"}, {"oct"}),  # the dotted form works too
        ({"direction": "income"}, {"pay"}),
        ({"direction": "expense"}, {"aug", "sep", "sep_car", "oct"}),
        ({"text": "LIDL"}, {"oct"}),  # case-insensitive
        ({"text": "żab"}, {"sep"}),  # Polish letters fold
        ({"text": "ŻABKA  WEEKLY"}, {"sep"}),  # whitespace collapses
        ({"text": "100%"}, {"sep_car"}),  # % is literal
        ({"text": "_"}, set()),  # _ is literal
        ({"text": "ay"}, {"pay"}),  # "contains", not "starts with"
        ({"amount_min": "12.50"}, {"sep", "sep_car", "oct", "pay"}),
        ({"amount_max": "12.5"}, {"aug", "oct"}),
        ({"amount_min": "80", "amount_max": "1,234.56"}, {"sep", "sep_car"}),
        ({"amount_min": "0"}, {"aug", "sep", "sep_car", "oct", "pay"}),
        ({"direction": "expense", "text": "a", "amount_min": "6"}, {"sep"}),
    ],
)
def test_filters(
    team: WorkspaceDb, ledger: dict[str, UUID], fields: dict[str, str], expected: set[str]
) -> None:
    rows = _search(team, _where(team, **fields)).rows
    names = {key for key, value in ledger.items() if value in {row.id for row in rows}}
    assert names == expected


def test_category_filters_take_several_categories_archived_included(
    team: WorkspaceDb, ledger: dict[str, UUID]
) -> None:
    where = _where(team, categories=(str(ledger["car"]), str(ledger["salary"])))
    assert {row.id for row in _search(team, where).rows} == {ledger["sep_car"], ledger["pay"]}
    unknown = _where(team, categories=(str(uuid4()),))
    assert _search(team, unknown).rows == ()


def test_amounts_are_parsed_in_the_users_locale(team: WorkspaceDb, ledger: dict) -> None:
    where = _where(team, "pl", amount_min="1 234,56")
    assert where.amount_min_minor == 123456
    assert _descriptions(_search(team, where)) == ["September pay", "Żabka weekly"]


@pytest.mark.parametrize(
    ("fields", "errors"),
    [
        ({"date_from": "7"}, {"date_from": "invalid_date"}),  # a bare day has no month here
        ({"date_from": "2026-02-30"}, {"date_from": "day_not_in_month"}),
        ({"date_to": "yesterday"}, {"date_to": "invalid_date"}),
        ({"date_to": "1899-12-31"}, {"date_to": "date_out_of_range"}),
        (
            {"date_from": "2026-10-02", "date_to": "2026-10-01"},
            {"date_to": "date_range_reversed"},
        ),
        ({"direction": "both"}, {"direction": "invalid_direction"}),
        ({"categories": ("food",)}, {"categories": "invalid_category"}),
        ({"text": "x" * 201}, {"text": "text_too_long"}),
        ({"amount_min": "abc"}, {"amount_min": "invalid_amount"}),
        ({"amount_min": "-5"}, {"amount_min": "amount_negative"}),
        ({"amount_max": "1.234"}, {"amount_max": "too_many_decimals"}),
        ({"amount_min": "10", "amount_max": "9.99"}, {"amount_max": "amount_range_reversed"}),
        (
            {"date_from": "x", "amount_max": "y", "direction": "z"},
            {
                "date_from": "invalid_date",
                "amount_max": "invalid_amount",
                "direction": "invalid_direction",
            },
        ),
    ],
)
def test_invalid_filters_report_every_field(fields: dict[str, Any], errors: dict[str, str]) -> None:
    assert _errors(**fields) == errors


def test_an_empty_filter_input_is_no_filter() -> None:
    assert FilterInput().is_empty
    assert FilterInput(text="  ").is_empty
    assert parse_filter(FilterInput(text="  "), locale="en", currency="EUR") == TransactionFilter()
    assert not FilterInput(direction="income").is_empty


# Sorting and pages


def test_each_sort_order_with_entry_order_breaking_ties(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    first = _add(team, food, "2026-10-05", "10", "first")
    second = _add(team, food, "2026-10-05", "10", "second")
    big = _add(team, food, "2026-10-01", "99", "big")
    late = _add(team, food, "2026-10-09", "1", "late")

    def order(sort: SortOrder) -> list[UUID]:
        return [row.id for row in _search(team, sort=sort).rows]

    assert order(SortOrder.DATE_DESC) == [late, second, first, big]
    assert order(SortOrder.DATE_ASC) == [big, first, second, late]
    assert order(SortOrder.AMOUNT_DESC) == [big, second, first, late]
    assert order(SortOrder.AMOUNT_ASC) == [late, first, second, big]


def test_pages_of_fifty(team: WorkspaceDb) -> None:
    food = _category(team, "Food")
    for n in range(101):
        _add(team, food, f"2026-{1 + n % 12:02d}-{1 + n % 28:02d}", str(n + 1), f"row {n}")
    seen: list[UUID] = []
    for number, size in ((1, 50), (2, 50), (3, 1)):
        result = _search(team, sort=SortOrder.AMOUNT_ASC, page=number)
        assert (result.window.number, len(result.rows), result.window.total) == (number, size, 101)
        seen += [row.id for row in result.rows]
    assert len(set(seen)) == 101  # no row twice, none missing
    assert _search(team, page=9).window.number == 3  # past the end: the last page
    assert _search(team, page=0).window.number == 1


def test_rows_in_closed_months_are_marked(team: WorkspaceDb, ledger: dict[str, UUID]) -> None:
    close_month(team.factory, team.ctx("olga", "owner"), YearMonth(2026, 9))
    closed = {row.id for row in _search(team).rows if row.month_closed}
    assert closed == {ledger["sep"], ledger["sep_car"], ledger["pay"]}


# Export


def _export(
    db: WorkspaceDb,
    where: TransactionFilter | None = None,
    options: CsvOptions | None = None,
    ctx: Context | None = None,
) -> tuple[int, bytes]:
    out = io.BytesIO()
    count = export_transactions_csv(
        db.factory, ctx or db.ctx("vic", "viewer"), where or TransactionFilter(),
        options or CsvOptions(), out,
    )  # fmt: skip
    return count, out.getvalue()


def test_the_export_writes_every_matching_row_in_entry_order(
    team: WorkspaceDb, ledger: dict[str, UUID]
) -> None:
    count, data = _export(team, _where(team, direction="expense"))
    assert count == 4
    text = data.decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(text, newline="")))
    assert [r["id"] for r in rows] == [
        str(ledger[k]) for k in ("aug", "sep", "sep_car", "oct")
    ]  # date order, not the screen's newest-first
    assert rows[1] == {
        "date": "2026-09-01",
        "direction": "expense",
        "amount": "1234.56",
        "currency": "EUR",
        "category": "Food",
        "description": "Żabka weekly",
        "category_id": str(ledger["food"]),
        "id": str(ledger["sep"]),
    }
    assert rows[2]["category"] == "Car"  # archived categories keep their name
    assert "Deleted entry" not in text
    assert text.count("\r\n") == 5 and not data.startswith(b"\xef\xbb\xbf")


def test_the_export_options(team: WorkspaceDb, ledger: dict[str, UUID]) -> None:
    options = CsvOptions(delimiter="semicolon", decimal="comma", bom=True)
    _count, data = _export(team, _where(team, text="żabka"), options)
    assert data.startswith(b"\xef\xbb\xbfdate;direction;amount;")
    assert ";1234,56;EUR;Food;" in data.decode("utf-8-sig")


def test_viewers_can_export_and_nothing_is_audited(
    team: WorkspaceDb, ledger: dict[str, UUID]
) -> None:
    with team.factory() as session:
        before = session.scalars(select(AuditEvent)).all()
    count, data = _export(team, ctx=team.ctx("vic", "viewer"))
    assert count == 5
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8"), newline="")))
    assert [r["id"] for r in rows] == [
        str(ledger[k]) for k in ("aug", "sep", "sep_car", "pay", "oct")
    ]
    assert [(r["description"], r["category"], r["amount"]) for r in rows] == [
        ("Bakery", "Food", "5.00"),
        ("Żabka weekly", "Food", "1234.56"),
        ("Fuel 100%", "Car", "80.00"),
        ("September pay", "Salary", "3400.00"),
        ("lidl", "Food", "12.50"),
    ]
    with team.factory() as session:
        assert len(session.scalars(select(AuditEvent)).all()) == len(before)
    assert service.EXPORT_ROLE is Role.VIEWER


def test_an_empty_result_exports_only_the_header(team: WorkspaceDb) -> None:
    count, data = _export(team)
    assert (count, data) == (
        0,
        b"date,direction,amount,currency,category,description,category_id,id\r\n",
    )


def test_another_workspace_is_never_exported(team: WorkspaceDb, ledger: dict) -> None:
    other = team.add_workspace("Other")
    team.add_member("ed", "editor", other)
    count, _data = _export(team, ctx=team.ctx("ed", "editor", other))
    assert count == 0


# Bulk recategorize


def _ids(*keys: str, ledger: dict[str, UUID]) -> Selection:
    return Selection(ids=frozenset(ledger[k] for k in keys))


def _apply(
    db: WorkspaceDb, selection: Selection, target: UUID, ctx: Context | None = None
) -> service.RecategorizePlan:
    ctx = ctx or _ed(db)
    plan = preview_recategorize(db.factory, ctx, selection, target)
    return recategorize_transactions(db.factory, ctx, selection, target, expected=plan.fingerprint)


def test_recategorize_selected_rows_with_one_audit_event(
    team: WorkspaceDb, ledger: dict[str, UUID]
) -> None:
    groceries = _category(team, "Groceries")
    selection = _ids("aug", "sep", "oct", ledger=ledger)
    plan = preview_recategorize(team.factory, _ed(team), selection, groceries)
    assert (plan.selected, plan.changing, plan.target.name) == (3, 3, "Groceries")
    assert plan.months == (YearMonth(2026, 8), YearMonth(2026, 9), YearMonth(2026, 10))
    assert plan.from_categories == ("Food",)
    assert _categories_of(team)[ledger["aug"]] == ledger["food"]  # the preview wrote nothing
    assert _events(team, "transaction.recategorized") == []

    done = recategorize_transactions(
        team.factory, _ed(team), selection, groceries, expected=plan.fingerprint
    )
    assert done.changing == 3
    stored = _categories_of(team)
    assert {stored[ledger[k]] for k in ("aug", "sep", "oct")} == {groceries}
    assert stored[ledger["sep_car"]] == ledger["car"]
    (event,) = _events(team, "transaction.recategorized")
    assert (event.entity_type, event.entity_id, event.actor_user_id) == (
        "category",
        str(groceries),
        team.users["ed"],
    )
    assert event.data == {
        "category": "Groceries",
        "direction": "expense",
        "count": 3,
        "from_categories": "Food",
        "months": "2026-08, 2026-09, 2026-10",
    }
    # Never amounts or descriptions.
    assert "amount" not in event.data and "Żabka" not in str(event.data)


def test_recategorize_all_filtered_rows(team: WorkspaceDb, ledger: dict[str, UUID]) -> None:
    other = _category(team, "Other")
    selection = Selection(where=_where(team, direction="expense", date_from="2026-09-01"))
    plan = _apply(team, selection, other)
    assert (plan.selected, plan.changing) == (3, 3)
    assert plan.from_categories == ("Car", "Food")  # an archived category may be left
    stored = _categories_of(team)
    assert {stored[ledger[k]] for k in ("sep", "sep_car", "oct")} == {other}
    assert stored[ledger["aug"]] == ledger["food"]


def test_rows_already_in_the_target_are_left_alone(
    team: WorkspaceDb, ledger: dict[str, UUID]
) -> None:
    plan = _apply(team, _ids("sep", "sep_car", ledger=ledger), ledger["food"])
    assert (plan.selected, plan.changing, plan.from_categories) == (2, 1, ("Car",))
    assert _events(team, "transaction.recategorized")[0].data["count"] == 1
    # Nothing to change: nothing is recorded.
    _apply(team, _ids("sep", ledger=ledger), ledger["food"])
    assert len(_events(team, "transaction.recategorized")) == 1


def test_viewers_cannot_recategorize(team: WorkspaceDb, ledger: dict[str, UUID]) -> None:
    viewer = team.ctx("vic", "viewer")
    selection = _ids("sep", ledger=ledger)
    with pytest.raises(PermissionDenied):
        preview_recategorize(team.factory, viewer, selection, ledger["food"])
    with pytest.raises(PermissionDenied):
        recategorize_transactions(team.factory, viewer, selection, ledger["food"], expected="x")


def test_a_demoted_editor_is_refused_inside_the_write(
    team: WorkspaceDb, ledger: dict[str, UUID]
) -> None:
    other = _category(team, "Other")
    selection = _ids("sep", ledger=ledger)
    stale = _ed(team)
    plan = preview_recategorize(team.factory, stale, selection, other)
    with team.factory.begin() as session:
        membership = session.get(Membership, team.memberships["ed"])
        assert membership is not None
        membership.role = "viewer"
    with pytest.raises(PermissionDenied):
        recategorize_transactions(team.factory, stale, selection, other, expected=plan.fingerprint)
    assert _categories_of(team)[ledger["sep"]] == ledger["food"]


def test_a_closed_month_refuses_the_whole_operation(
    team: WorkspaceDb, ledger: dict[str, UUID]
) -> None:
    other = _category(team, "Other")
    selection = _ids("aug", "sep", "oct", ledger=ledger)
    plan = preview_recategorize(team.factory, _ed(team), selection, other)
    owner = team.ctx("olga", "owner")
    close_month(team.factory, owner, YearMonth(2026, 9))
    close_month(team.factory, owner, YearMonth(2026, 8))
    before = _categories_of(team)
    with pytest.raises(MonthsClosed) as caught:
        recategorize_transactions(
            team.factory, _ed(team), selection, other, expected=plan.fingerprint
        )
    assert caught.value.months == (YearMonth(2026, 8), YearMonth(2026, 9))
    assert _categories_of(team) == before  # no row changed
    assert _events(team, "transaction.recategorized") == []
    # The preview refuses the same way.
    with pytest.raises(MonthsClosed):
        preview_recategorize(team.factory, _ed(team), selection, other)
    # A row in a closed month counts even when it is already in the target category.
    with pytest.raises(MonthsClosed):
        preview_recategorize(team.factory, _ed(team), _ids("sep", ledger=ledger), ledger["food"])


def test_the_month_lock_is_taken_before_the_rows_are_read(
    team: WorkspaceDb, ledger: dict[str, UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    original_lock = service.lock_month_writes
    original_read = service.TransactionRepository.matching_for_update

    def lock(session: Any, ctx: Context) -> None:
        calls.append("lock")
        original_lock(session, ctx)

    def read(self: Any, **kwargs: Any) -> Any:
        calls.append("read")
        return original_read(self, **kwargs)

    other = _category(team, "Other")
    selection = _ids("sep", ledger=ledger)
    plan = preview_recategorize(team.factory, _ed(team), selection, other)
    monkeypatch.setattr(service, "lock_month_writes", lock)
    monkeypatch.setattr(service.TransactionRepository, "matching_for_update", read)
    recategorize_transactions(team.factory, _ed(team), selection, other, expected=plan.fingerprint)
    assert calls == ["lock", "read"]


def test_the_target_must_have_the_rows_kind(team: WorkspaceDb, ledger: dict[str, UUID]) -> None:
    with pytest.raises(KindMismatch):
        preview_recategorize(team.factory, _ed(team), _ids("sep", "pay", ledger=ledger),
                             ledger["food"])  # fmt: skip
    with pytest.raises(KindMismatch):
        preview_recategorize(team.factory, _ed(team), _ids("pay", ledger=ledger), ledger["food"])
    mixed = Selection(where=TransactionFilter())
    plan = preview_recategorize(
        team.factory, _ed(team), Selection(where=_where(team, direction="income")), ledger["salary"]
    )
    assert plan.changing == 0
    with pytest.raises(KindMismatch):
        preview_recategorize(team.factory, _ed(team), mixed, ledger["food"])


def test_archived_unknown_or_foreign_targets_are_refused(
    team: WorkspaceDb, ledger: dict[str, UUID]
) -> None:
    other_ws = team.add_workspace("Other")
    team.add_member("ed", "editor", other_ws)
    foreign = create_category(
        team.factory, team.ctx("ed", "editor", other_ws), name="Food", kind="expense",
        treatment="regular", color="#16a34a",
    ).id  # fmt: skip
    for target in (ledger["car"], uuid4(), foreign):  # archived, unknown, another workspace's
        with pytest.raises(InvalidTarget):
            preview_recategorize(team.factory, _ed(team), _ids("sep", ledger=ledger), target)


def test_empty_or_vanished_selections(team: WorkspaceDb, ledger: dict[str, UUID]) -> None:
    with pytest.raises(NothingSelected):
        preview_recategorize(team.factory, _ed(team), Selection(ids=frozenset()), ledger["food"])
    with pytest.raises(NothingSelected):  # unknown ids select nothing
        preview_recategorize(
            team.factory, _ed(team), Selection(ids=frozenset({uuid4()})), ledger["food"]
        )
    nothing = Selection(where=_where(team, text="no such entry"))
    with pytest.raises(NothingSelected):
        preview_recategorize(team.factory, _ed(team), nothing, ledger["food"])
    too_many = Selection(ids=frozenset(uuid4() for _ in range(MAX_SELECTED_IDS + 1)))
    with pytest.raises(TooManyTicked) as caught:
        preview_recategorize(team.factory, _ed(team), too_many, ledger["food"])
    assert caught.value.code == "too_many_ticked"
    at_most = Selection(
        ids=frozenset([ledger["sep"], *(uuid4() for _ in range(MAX_SELECTED_IDS - 1))])
    )
    assert preview_recategorize(team.factory, _ed(team), at_most, ledger["food"]).selected == 1


def test_the_row_cap(
    team: WorkspaceDb, ledger: dict[str, UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "MAX_RECATEGORIZE_ROWS", 3)
    expenses = Selection(where=_where(team, direction="expense"))  # 4 rows
    with pytest.raises(TooManySelected):
        preview_recategorize(team.factory, _ed(team), expenses, ledger["food"])
    three = Selection(where=_where(team, direction="expense", date_from="2026-09-01"))
    assert preview_recategorize(team.factory, _ed(team), three, ledger["food"]).selected == 3


def _spy_reads(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    original = service.TransactionRepository.matching_for_update

    def read(self: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return original(self, **kwargs)

    monkeypatch.setattr(service.TransactionRepository, "matching_for_update", read)
    return calls


def test_at_most_the_cap_plus_one_rows_are_loaded(
    team: WorkspaceDb, ledger: dict[str, UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "MAX_RECATEGORIZE_ROWS", 2)
    calls = _spy_reads(monkeypatch)
    with pytest.raises(TooManySelected):
        preview_recategorize(
            team.factory, _ed(team), Selection(where=_where(team, direction="expense")),
            ledger["food"],
        )  # fmt: skip
    assert calls[-1]["limit"] == 3
    statements = _statements(
        lambda: preview_recategorize(
            team.factory, _ed(team), _ids("sep", ledger=ledger), ledger["food"]
        )
    )
    read = next(s for s in statements if "FROM transactions" in s)
    assert "LIMIT" in read


def test_the_preview_takes_no_row_locks_and_the_change_does(
    team: WorkspaceDb, ledger: dict[str, UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    other = _category(team, "Other")
    selection = _ids("sep", "oct", ledger=ledger)
    calls = _spy_reads(monkeypatch)
    preview: list[service.RecategorizePlan] = []
    statements = _statements(
        lambda: preview.append(preview_recategorize(team.factory, _ed(team), selection, other))
    )
    assert calls[-1]["lock"] is False
    assert not any("FOR UPDATE" in s or "FOR SHARE" in s for s in statements)
    statements = _statements(
        lambda: recategorize_transactions(
            team.factory, _ed(team), selection, other, expected=preview[0].fingerprint
        )
    )
    assert calls[-1]["lock"] is True
    read = next(s for s in statements if "FROM transactions" in s and "JOIN categories" in s)
    # Only the transactions are locked, not the categories they are joined with.
    assert read.rstrip().endswith("FOR UPDATE OF transactions")


def test_a_selection_that_changed_after_the_preview_is_refused(
    team: WorkspaceDb, ledger: dict[str, UUID]
) -> None:
    other = _category(team, "Other")
    selection = Selection(where=_where(team, text="a", direction="expense"))
    plan = preview_recategorize(team.factory, _ed(team), selection, other)
    _add(team, ledger["food"], "2026-10-03", "1", "Banana")  # now matches too
    with pytest.raises(SelectionChanged):
        recategorize_transactions(team.factory, _ed(team), selection, other,
                                  expected=plan.fingerprint)  # fmt: skip
    assert _events(team, "transaction.recategorized") == []
    ids = _ids("sep", "oct", ledger=ledger)
    plan = preview_recategorize(team.factory, _ed(team), ids, other)
    delete_transaction(team.factory, _ed(team), ledger["oct"])
    with pytest.raises(SelectionChanged):
        recategorize_transactions(team.factory, _ed(team), ids, other, expected=plan.fingerprint)
    assert _categories_of(team)[ledger["sep"]] == ledger["food"]


def test_deleted_and_foreign_ids_are_neither_selected_nor_touched(
    team: WorkspaceDb, ledger: dict[str, UUID]
) -> None:
    other = _category(team, "Other")
    with team.factory() as session:
        gone = session.scalars(
            select(Transaction).where(Transaction.description == "Deleted entry")
        ).one()
        gone_id, gone_stamp = gone.id, gone.updated_at
    elsewhere = team.add_workspace("Elsewhere")
    team.add_member("ed", "editor", elsewhere)
    there = team.ctx("ed", "editor", elsewhere)
    foreign_food = create_category(
        team.factory, there, name="Food", kind="expense", treatment="regular", color="#16a34a"
    ).id
    foreign = create_transaction(
        team.factory, there, month=YearMonth(2026, 10), direction="expense", day="3",
        amount="1", description="Theirs", category_id=foreign_food, locale="en",
    ).id  # fmt: skip
    with team.factory() as session:
        row = session.get(Transaction, foreign)
        assert row is not None
        foreign_stamp = row.updated_at
    selection = Selection(ids=frozenset({ledger["sep"], gone_id, foreign}))
    plan = preview_recategorize(team.factory, _ed(team), selection, other)
    assert (plan.selected, plan.changing) == (1, 1)
    done = recategorize_transactions(
        team.factory, _ed(team), selection, other, expected=plan.fingerprint
    )
    assert done.selected == 1
    with team.factory() as session:
        deleted_row = session.get(Transaction, gone_id)
        foreign_row = session.get(Transaction, foreign)
        assert deleted_row is not None and foreign_row is not None
        assert (deleted_row.category_id, deleted_row.updated_at) == (ledger["food"], gone_stamp)
        assert (foreign_row.category_id, foreign_row.updated_at) == (foreign_food, foreign_stamp)
    assert _categories_of(team)[ledger["sep"]] == other


def test_a_row_moved_out_of_the_filter_changes_the_selection(
    team: WorkspaceDb, ledger: dict[str, UUID]
) -> None:
    other = _category(team, "Other")
    selection = Selection(where=_where(team, direction="expense", date_from="2026-09-01"))
    plan = preview_recategorize(team.factory, _ed(team), selection, other)
    assert plan.selected == 3
    update_transaction(
        team.factory, _ed(team), ledger["oct"], month=YearMonth(2026, 10), day="2026-08-15",
        amount="12.50", description="lidl", category_id=ledger["food"], locale="en",
    )  # fmt: skip
    with pytest.raises(SelectionChanged):
        recategorize_transactions(
            team.factory, _ed(team), selection, other, expected=plan.fingerprint
        )
    assert {_categories_of(team)[ledger[k]] for k in ("sep", "sep_car")} == {
        ledger["food"],
        ledger["car"],
    }


def test_a_category_changed_by_someone_else_after_the_preview_is_refused(
    team: WorkspaceDb, ledger: dict[str, UUID]
) -> None:
    other, groceries = _category(team, "Other"), _category(team, "Groceries")
    selection = _ids("sep", "oct", ledger=ledger)
    plan = preview_recategorize(team.factory, _ed(team), selection, other)
    # Someone else moves one of the rows meanwhile (same ids, so only the category differs).
    _apply(team, _ids("oct", ledger=ledger), groceries, ctx=team.ctx("olga", "owner"))
    with pytest.raises(SelectionChanged):
        recategorize_transactions(
            team.factory, _ed(team), selection, other, expected=plan.fingerprint
        )
    assert _categories_of(team)[ledger["oct"]] == groceries
    assert _categories_of(team)[ledger["sep"]] == ledger["food"]


def test_the_fingerprint_covers_id_category_and_date() -> None:
    row, cat, day = uuid4(), uuid4(), date(2026, 10, 1)
    base = service.fingerprint([(row, cat, day)])
    assert service.fingerprint([(row, cat, day)]) == base
    assert service.fingerprint([(row, uuid4(), day)]) != base
    assert service.fingerprint([(row, cat, date(2026, 10, 2))]) != base
    assert service.fingerprint([(uuid4(), cat, day)]) != base
    other = (uuid4(), cat, day)
    assert service.fingerprint([(row, cat, day), other]) == service.fingerprint(
        [other, (row, cat, day)]
    )


def test_updated_at_moves_with_the_change(team: WorkspaceDb, ledger: dict[str, UUID]) -> None:
    with team.factory() as session:
        before = session.get(Transaction, ledger["sep"])
        assert before is not None
        stamp = before.updated_at
    _apply(team, _ids("sep", ledger=ledger), _category(team, "Other"))
    with team.factory() as session:
        after = session.get(Transaction, ledger["sep"])
        assert after is not None and after.updated_at > stamp
