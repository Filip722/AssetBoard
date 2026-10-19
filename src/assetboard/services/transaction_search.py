"""Transaction search, CSV export and bulk recategorize (F2.6).

* **Search** (every member): non-deleted transactions of every month that match a
  ``TransactionFilter``, one page (``PAGE_SIZE``) at a time, in a chosen order.
* **Export** (``EXPORT_ROLE``; owner decision 2026-10-19: viewers too): every matching row in
  entry order, as M4's ``transactions.csv`` columns (``domain.ledger_csv``). It is a read: it is
  not audited (owner decision 2026-10-19), and nothing about its contents is logged.
* **Bulk recategorize** (editors and owners): moves the selected rows (ids from one page) or
  every matching row to one category. A preview reads what would change; the change runs in one
  ``BEGIN IMMEDIATE`` transaction that re-reads the actor's role (``require_role_now``), takes
  ``lock_month_writes`` **before** reading the rows, and refuses the whole operation when any
  selected row lies in a closed month (``MonthsClosed`` lists them). The preview reads without
  row locks. The preview's ``fingerprint`` (a digest of each selected row's id, category and
  date) must still match, so rows deleted, added, moved or recategorized after the preview are
  never changed unseen (``SelectionChanged``). At most ``MAX_RECATEGORIZE_ROWS`` rows are ever
  loaded (one more is read to detect "too many"). One
  ``transaction.recategorized`` event is audited, without amounts or descriptions.

Filters arrive as the user typed them; ``parse_filter`` validates them (amounts in the user's
locale, refused rather than rounded) and collects every error per field (``InvalidFilter``).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.ledger import Category, Transaction
from assetboard.db.repositories import (
    AuditEventRepository,
    CategoryRepository,
    MonthStateRepository,
    TransactionRepository,
)
from assetboard.domain.ledger_csv import CsvOptions, TransactionCsvRow, transactions_csv
from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.domain.transaction_search import (
    MAX_SEARCH_TEXT_LENGTH,
    PAGE_SIZE,
    PageWindow,
    SortOrder,
    TransactionFilter,
    page_window,
    search_key,
)
from assetboard.domain.transactions import (
    EntryError,
    TransactionDirection,
    amount_minor,
    parse_entry_date,
)
from assetboard.i18n.numbers import parse_amount
from assetboard.services.context import Context, Role, require_role, require_role_now
from assetboard.services.months import lock_month_writes

# Owner decision (2026-10-19): every member who can read the list can export it.
EXPORT_ROLE = Role.VIEWER
RECATEGORIZE_ROLE = Role.EDITOR
# At most this many rows in one bulk change (a code constant; the export has no limit).
MAX_RECATEGORIZE_ROWS = 10_000
# Selected rows come from one page of checkboxes; this leaves room and bounds the IN list.
MAX_SELECTED_IDS = 500

_DAY_ONLY = re.compile(r"[0-9]{1,2}")
_ANY_MONTH = YearMonth(2000, 1)  # parse_entry_date needs one; bare days are refused first


# Filters


@dataclass(frozen=True, slots=True)
class FilterInput:
    """The filter fields as typed (query parameters or hidden form fields)."""

    date_from: str = ""
    date_to: str = ""
    direction: str = ""
    categories: tuple[str, ...] = ()
    text: str = ""
    amount_min: str = ""
    amount_max: str = ""

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.date_from.strip(),
                self.date_to.strip(),
                self.direction.strip(),
                self.categories,
                self.text.strip(),
                self.amount_min.strip(),
                self.amount_max.strip(),
            )
        )


class SearchError(Exception):
    code = "invalid"


class InvalidFilter(SearchError):
    """One or more filter fields are invalid; ``errors`` maps field to error code.

    Fields: ``date_from``, ``date_to``, ``direction``, ``categories``, ``text``,
    ``amount_min``, ``amount_max`` (and ``options`` for the export's CSV options).
    """

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__(", ".join(f"{field}={code}" for field, code in errors.items()))
        self.errors = errors


def _date(value: str) -> date:
    text = value.strip()
    if _DAY_ONLY.fullmatch(text):  # a bare day has no month here
        raise EntryError("invalid_date")
    return parse_entry_date(text, _ANY_MONTH)


def _bound(value: str, locale: str, currency: str) -> int:
    amount = parse_amount(value, locale)  # ValueError: not a number
    if amount == 0:
        return 0
    if amount < 0:
        raise EntryError("amount_negative")
    return amount_minor(amount, currency)


def parse_filter(raw: FilterInput, *, locale: str, currency: str) -> TransactionFilter:
    """Validate the typed filter; raises ``InvalidFilter`` with every problem found."""
    errors: dict[str, str] = {}
    dates: dict[str, date | None] = {"date_from": None, "date_to": None}
    for field, value in (("date_from", raw.date_from), ("date_to", raw.date_to)):
        if value.strip():
            try:
                dates[field] = _date(value)
            except EntryError as exc:
                errors[field] = exc.code
    direction: str | None = raw.direction.strip() or None
    if direction is not None and direction not in {d.value for d in TransactionDirection}:
        errors["direction"] = "invalid_direction"
    categories: set[UUID] = set()
    for value in raw.categories:
        if not value.strip():
            continue
        try:
            categories.add(UUID(value.strip()))
        except ValueError:
            errors["categories"] = "invalid_category"
    if len(raw.text) > MAX_SEARCH_TEXT_LENGTH:
        errors["text"] = "text_too_long"
    bounds: dict[str, int | None] = {"amount_min": None, "amount_max": None}
    for field, value in (("amount_min", raw.amount_min), ("amount_max", raw.amount_max)):
        if value.strip():
            try:
                bounds[field] = _bound(value, locale, currency)
            except EntryError as exc:
                errors[field] = exc.code
            except ValueError:
                errors[field] = "invalid_amount"
    low, high = dates["date_from"], dates["date_to"]
    if low is not None and high is not None and low > high:
        errors["date_to"] = "date_range_reversed"
    least, most = bounds["amount_min"], bounds["amount_max"]
    if least is not None and most is not None and least > most:
        errors["amount_max"] = "amount_range_reversed"
    if errors:
        raise InvalidFilter(errors)
    return TransactionFilter(
        date_from=low,
        date_to=high,
        direction=direction,
        category_ids=frozenset(categories),
        text_key=search_key(raw.text),
        amount_min_minor=least,
        amount_max_minor=most,
    )


# Search


@dataclass(frozen=True, slots=True)
class CategoryOption:
    id: UUID
    name: str
    kind: str
    color: str
    archived: bool


@dataclass(frozen=True, slots=True)
class SearchRow:
    id: UUID
    date: date
    direction: str
    amount: Money
    description: str
    category_id: UUID
    category_name: str
    category_color: str
    category_archived: bool
    month_closed: bool

    @property
    def month(self) -> YearMonth:
        return YearMonth.from_date(self.date)


@dataclass(frozen=True, slots=True)
class SearchResult:
    rows: tuple[SearchRow, ...]
    window: PageWindow
    sort: SortOrder
    categories: tuple[CategoryOption, ...]  # every category, archived ones included


def _option(category: Category) -> CategoryOption:
    return CategoryOption(
        id=category.id,
        name=category.name,
        kind=category.kind,
        color=category.color,
        archived=category.archived_at is not None,
    )


def category_options(factory: sessionmaker[Session], ctx: Context) -> tuple[CategoryOption, ...]:
    """Every category (by kind, display order; archived included), for the filter."""
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        return tuple(_option(c) for c in CategoryRepository(session, ctx).ordered())


def search_transactions(
    factory: sessionmaker[Session],
    ctx: Context,
    where: TransactionFilter,
    *,
    sort: SortOrder,
    page: int,
) -> SearchResult:
    """One page of matching rows; a page past the end shows the last page."""
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        repository = TransactionRepository(session, ctx)
        window = page_window(page, repository.count_matching(where), PAGE_SIZE)
        rows = repository.search(where, sort=sort, limit=window.size, offset=window.offset)
        categories = CategoryRepository(session, ctx).ordered()
        closed = MonthStateRepository(session, ctx).closed_months()
        # Each row's category comes from the row's own statement (no lookup that could miss).
        views = tuple(_row(row, category, closed) for row, category in rows)
    return SearchResult(views, window, sort, tuple(_option(c) for c in categories))


def _row(row: Transaction, category: Category, closed: Collection[YearMonth]) -> SearchRow:
    return SearchRow(
        id=row.id,
        direction=row.direction,
        date=row.date,
        amount=Money(row.amount_minor, row.currency),
        description=row.description,
        category_id=row.category_id,
        category_name=category.name,
        category_color=category.color,
        category_archived=category.archived_at is not None,
        month_closed=YearMonth.from_date(row.date) in closed,
    )


# Export


class ByteSink(Protocol):
    def write(self, data: bytes, /) -> object: ...


def export_transactions_csv(
    factory: sessionmaker[Session],
    ctx: Context,
    where: TransactionFilter,
    options: CsvOptions,
    out: ByteSink,
) -> int:
    """Write every matching row to ``out`` as UTF-8 CSV (entry order); returns the row count.

    The rows and their category names come from **one** SELECT statement, read in batches, so
    every row carries the category name it had in the same read. What that statement sees of
    writes committed while a long export runs is up to the database's isolation (no stronger
    isolation is requested); the count and the file always agree.
    """
    require_role(ctx, EXPORT_ROLE)
    written = 0
    with factory() as session:

        def rows() -> Iterator[TransactionCsvRow]:
            nonlocal written
            for row, category in TransactionRepository(session, ctx).iter_matching(where):
                written += 1
                yield TransactionCsvRow(
                    date=row.date,
                    direction=row.direction,
                    amount_minor=row.amount_minor,
                    currency=row.currency,
                    category=category.name,
                    description=row.description,
                    category_id=row.category_id,
                    id=row.id,
                )

        for chunk in transactions_csv(rows(), options):
            out.write(chunk.encode("utf-8"))
    return written


# Bulk recategorize


@dataclass(frozen=True, slots=True)
class Selection:
    """The rows a bulk change targets: ``ids`` (checked on one page) or every ``where`` match."""

    ids: frozenset[UUID] | None = None
    where: TransactionFilter | None = None

    def __post_init__(self) -> None:
        if (self.ids is None) == (self.where is None):
            raise ValueError("a selection has either ids or a filter")


class RecategorizeError(SearchError):
    pass


class NothingSelected(RecategorizeError):
    code = "nothing_selected"


class TooManySelected(RecategorizeError):
    """More rows than ``MAX_RECATEGORIZE_ROWS`` match."""

    code = "too_many_rows"


class TooManyTicked(RecategorizeError):
    """More than ``MAX_SELECTED_IDS`` rows were ticked."""

    code = "too_many_ticked"


class InvalidTarget(RecategorizeError):
    """The target category is missing, of another workspace, or archived."""

    code = "invalid_target"


class KindMismatch(RecategorizeError):
    """A selected row's direction differs from the target category's kind."""

    code = "kind_mismatch"


class SelectionChanged(RecategorizeError):
    """The selected rows are not the ones the preview showed (changed meanwhile)."""

    code = "selection_changed"


class MonthsClosed(RecategorizeError):
    """Selected rows lie in closed months (oldest first); nothing was changed."""

    code = "months_closed"

    def __init__(self, months: Sequence[YearMonth]) -> None:
        super().__init__(", ".join(str(m) for m in months))
        self.months = tuple(months)


@dataclass(frozen=True, slots=True)
class RecategorizePlan:
    """What a bulk change would do (the preview) or did."""

    target: CategoryOption
    selected: int  # rows in the selection
    changing: int  # of those, rows not already in the target category
    months: tuple[YearMonth, ...]  # months of the changing rows, oldest first
    from_categories: tuple[str, ...]  # names of the categories the changing rows leave
    fingerprint: str


def fingerprint(rows: Collection[tuple[UUID, UUID, date]]) -> str:
    """A digest of the selected rows' (id, category id, date); order does not matter."""
    digest = hashlib.sha256()
    for row_id, category_id, day in sorted(rows):
        digest.update(row_id.bytes + category_id.bytes + day.isoformat().encode("ascii"))
    return digest.hexdigest()[:32]


def _fingerprint(rows: Sequence[tuple[Transaction, Category]]) -> str:
    return fingerprint([(row.id, row.category_id, row.date) for row, _category in rows])


def _selected(
    session: Session, ctx: Context, selection: Selection, *, lock: bool
) -> list[tuple[Transaction, Category]]:
    if selection.ids is not None and not selection.ids:
        raise NothingSelected
    if selection.ids is not None and len(selection.ids) > MAX_SELECTED_IDS:
        raise TooManyTicked
    cap = MAX_RECATEGORIZE_ROWS
    rows = TransactionRepository(session, ctx).matching_for_update(
        where=selection.where, ids=selection.ids, limit=cap + 1, lock=lock
    )
    if not rows:
        raise NothingSelected
    if len(rows) > cap:
        raise TooManySelected
    return rows


def _plan(
    session: Session, ctx: Context, rows: Sequence[tuple[Transaction, Category]], target_id: UUID
) -> tuple[RecategorizePlan, Category, list[Transaction]]:
    """Check the rows against the target and the closed months (refusing as a whole)."""
    target = CategoryRepository(session, ctx).get(target_id)
    if target is None or target.archived_at is not None:
        raise InvalidTarget
    if any(row.direction != target.kind for row, _category in rows):
        raise KindMismatch
    closed = MonthStateRepository(session, ctx).closed_months()
    in_closed = sorted({YearMonth.from_date(row.date) for row, _category in rows} & closed)
    if in_closed:
        raise MonthsClosed(in_closed)
    changing = [(row, category) for row, category in rows if row.category_id != target.id]
    plan = RecategorizePlan(
        target=_option(target),
        selected=len(rows),
        changing=len(changing),
        months=tuple(sorted({YearMonth.from_date(row.date) for row, _category in changing})),
        from_categories=tuple(
            sorted({category.name for _row, category in changing}, key=str.casefold)
        ),
        fingerprint=_fingerprint(rows),
    )
    return plan, target, [row for row, _category in changing]


def preview_recategorize(
    factory: sessionmaker[Session], ctx: Context, selection: Selection, target_id: UUID
) -> RecategorizePlan:
    """What ``recategorize_transactions`` would change; refuses as it would. Writes nothing."""
    require_role(ctx, RECATEGORIZE_ROLE)
    with factory() as session:
        rows = _selected(session, ctx, selection, lock=False)  # a read: no row locks
        return _plan(session, ctx, rows, target_id)[0]


def recategorize_transactions(
    factory: sessionmaker[Session],
    ctx: Context,
    selection: Selection,
    target_id: UUID,
    *,
    expected: str,
) -> RecategorizePlan:
    """Move the selected rows to ``target_id``, all or nothing, and audit it once.

    ``expected`` is the preview's fingerprint. Rows already in the target stay as they are;
    when no row changes, nothing is recorded.
    """
    require_role(ctx, RECATEGORIZE_ROLE)

    def write(session: Session) -> RecategorizePlan:
        require_role_now(session, ctx, RECATEGORIZE_ROLE)
        # The lock comes before reading the rows whose months the guard checks (F2.5).
        lock_month_writes(session, ctx)
        rows = _selected(session, ctx, selection, lock=True)
        if _fingerprint(rows) != expected:
            raise SelectionChanged
        plan, target, changing = _plan(session, ctx, rows, target_id)
        if not changing:
            return plan
        for row in changing:  # attribute changes keep the timestamp listener (no bulk UPDATE)
            row.category_id = target.id
        session.flush()
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="transaction.recategorized",
            entity_type="category",
            entity_id=str(target.id),
            ts=ctx.clock.now(),
            data={
                "category": target.name,
                "direction": target.kind,
                "count": plan.changing,
                "from_categories": ", ".join(plan.from_categories),
                "months": ", ".join(str(m) for m in plan.months),
            },
        )
        return plan

    return run_immediate(factory, write)
