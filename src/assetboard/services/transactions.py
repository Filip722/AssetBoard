"""Transactions (F2.2 part A): create, edit, soft delete, undo, month lists and autocomplete.

Every member may read; writes need ``editor``. Each write runs in one ``BEGIN IMMEDIATE``
transaction (``run_immediate``) that re-reads the actor's role (``require_role_now``) and then
follows the closed-month guard of F2.5 before changing anything:

* create: ``ensure_date_open`` for the new date;
* edit: ``lock_month_writes`` **before** reading the stored row, then ``ensure_date_open`` for
  its old date and, when the month changes, for the new one;
* soft delete and undo: ``lock_month_writes``, read the row, ``ensure_date_open`` for its date.

Edit, delete and undo read the stored row with ``get_for_update`` (``FOR UPDATE`` on
PostgreSQL, after the workspace lock), so concurrent writes to the same row are serialised.

Amounts are positive minor units in the workspace's base currency, read from the workspace row
inside the write (after the lock), not from the possibly stale ``Context``. Once any
transaction exists (soft-deleted ones included) the base currency cannot change
(``workspace_has_transactions`` is one of ``services.workspaces.BASE_CURRENCY_LOCKS``).

A soft-deleted row can be restored by any editor or owner within ``UNDO_WINDOW`` (10 s) of its
deletion, measured with the injected clock. Writes are audited (``transaction.*``) with the
date, direction, category name and amount, never the description: audit rows are append-only,
so free text written there could never be erased.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.ledger import Category, Transaction
from assetboard.db.repositories import (
    AuditEventRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    DescriptionMatch,
    NotFound,
    TransactionRepository,
)
from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.domain.transactions import (
    UNDO_WINDOW,
    EntryError,
    Source,
    TransactionDirection,
    amount_minor,
    clean_description,
    description_key,
    parse_entry_date,
)
from assetboard.i18n.numbers import parse_amount
from assetboard.services.category_usage import CategoryUsage
from assetboard.services.context import Context, Role, require_role, require_role_now
from assetboard.services.months import ensure_date_open, lock_month_writes

SUGGESTION_LIMIT = 8


class TransactionError(Exception):
    code = "invalid"


class InvalidTransaction(TransactionError):
    """One or more fields are invalid; ``errors`` maps field to error code.

    Fields: ``direction``, ``day``, ``amount``, ``description``, ``category``.
    """

    def __init__(self, errors: Mapping[str, str]) -> None:
        super().__init__(", ".join(f"{field}={code}" for field, code in errors.items()))
        self.errors = dict(errors)


class UndoExpired(TransactionError):
    """The undo window (10 seconds after the deletion) has passed."""

    code = "undo_expired"


@dataclass(frozen=True, slots=True)
class TransactionView:
    id: UUID
    direction: str
    date: date
    amount: Money
    description: str
    category_id: UUID
    category_name: str
    category_color: str
    category_archived: bool
    source: str

    @property
    def month(self) -> YearMonth:
        return YearMonth.from_date(self.date)


@dataclass(frozen=True, slots=True)
class MonthTransactions:
    month: YearMonth
    expense: tuple[TransactionView, ...]
    income: tuple[TransactionView, ...]


@dataclass(frozen=True, slots=True)
class DeletedTransaction:
    id: UUID
    month: YearMonth
    undo_until: datetime  # UTC; undo is refused after this


@dataclass(frozen=True, slots=True)
class Suggestion:
    description: str
    category_id: UUID | None  # None: every category it was used with is archived


@dataclass(frozen=True, slots=True)
class _Entry:
    day: date
    amount: Decimal
    description: str
    key: str


# Validation (before any database work)


def _direction(value: str) -> TransactionDirection:
    try:
        return TransactionDirection(value)
    except ValueError:
        raise InvalidTransaction({"direction": "invalid_direction"}) from None


def _entry(
    *,
    month: YearMonth,
    day: str,
    amount: str,
    description: str,
    category_id: UUID | None,
    locale: str,
    currency: str,
) -> _Entry:
    """Parse every field, collecting all errors (the form shows them together)."""
    errors: dict[str, str] = {}
    parsed_day: date | None = None
    parsed_amount: Decimal | None = None
    cleaned = ""
    try:
        parsed_day = parse_entry_date(day, month)
    except EntryError as exc:
        errors["day"] = exc.code
    try:
        parsed_amount = parse_amount(amount, locale)
    except ValueError:
        errors["amount"] = "invalid_amount"
    else:
        try:
            amount_minor(parsed_amount, currency)
        except EntryError as exc:
            errors["amount"] = exc.code
    try:
        cleaned = clean_description(description)
    except EntryError as exc:
        errors["description"] = exc.code
    if category_id is None:
        errors["category"] = "category_required"
    if errors or parsed_day is None or parsed_amount is None:
        raise InvalidTransaction(errors)
    return _Entry(parsed_day, parsed_amount, cleaned, description_key(cleaned))


def _minor(amount: Decimal, currency: str) -> int:
    try:
        return amount_minor(amount, currency)
    except EntryError as exc:
        raise InvalidTransaction({"amount": exc.code}) from None


def _category(
    session: Session,
    ctx: Context,
    category_id: UUID,
    direction: str,
    *,
    keep: UUID | None = None,
) -> Category:
    """The category for an entry: same workspace, kind = direction, active (unless kept)."""
    category = CategoryRepository(session, ctx).get(category_id)
    if category is None:
        raise InvalidTransaction({"category": "invalid_category"})
    if category.kind != direction:
        raise InvalidTransaction({"category": "category_kind_mismatch"})
    if category.archived_at is not None and category.id != keep:
        raise InvalidTransaction({"category": "category_archived"})
    return category


# Views and audit


def _view(row: Transaction, category: Category) -> TransactionView:
    return TransactionView(
        id=row.id,
        direction=row.direction,
        date=row.date,
        amount=Money(row.amount_minor, row.currency),
        description=row.description,
        category_id=row.category_id,
        category_name=category.name,
        category_color=category.color,
        category_archived=category.archived_at is not None,
        source=row.source,
    )


def _amount_text(minor: int, currency: str) -> str:
    """The amount as a plain decimal (``12.50``), as stored in audit data."""
    return str(Money(minor, currency).to_decimal())


def _audit(
    session: Session,
    ctx: Context,
    action: str,
    row: Transaction,
    category: Category,
    extra: Mapping[str, str] | None = None,
) -> None:
    data: dict[str, str | int | bool] = {
        "date": row.date.isoformat(),
        "direction": row.direction,
        "category": category.name,
        "amount": _amount_text(row.amount_minor, row.currency),
        "currency": row.currency,
    }
    data |= dict(extra or {})
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action=action,
        entity_type="transaction",
        entity_id=str(row.id),
        ts=ctx.clock.now(),
        data=data,
    )


def _live_row(
    repository: TransactionRepository, transaction_id: UUID, *, lock: bool = False
) -> Transaction:
    """A non-deleted row of this workspace; ``NotFound`` otherwise."""
    row = repository.get_for_update(transaction_id) if lock else repository.get(transaction_id)
    if row is None or row.deleted_at is not None:
        raise NotFound
    return row


# Reads (every member)


def list_month(factory: sessionmaker[Session], ctx: Context, month: YearMonth) -> MonthTransactions:
    """The month's non-deleted transactions, by date and then entry order."""
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        rows = TransactionRepository(session, ctx).for_month(month)
        categories = {c.id: c for c in CategoryRepository(session, ctx).ordered()}
        views = [_view(row, categories[row.category_id]) for row in rows]
    return MonthTransactions(
        month=month,
        expense=tuple(v for v in views if v.direction == TransactionDirection.EXPENSE),
        income=tuple(v for v in views if v.direction == TransactionDirection.INCOME),
    )


def get_transaction(
    factory: sessionmaker[Session], ctx: Context, transaction_id: UUID
) -> TransactionView:
    """One non-deleted transaction; ``NotFound`` otherwise (also for another workspace's)."""
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        row = _live_row(TransactionRepository(session, ctx), transaction_id)
        category = CategoryRepository(session, ctx).get_or_raise(row.category_id)
        return _view(row, category)


def suggest_descriptions(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    direction: str,
    prefix: str,
    limit: int = SUGGESTION_LIMIT,
) -> list[Suggestion]:
    """Earlier descriptions of ``direction`` starting with ``prefix`` (any case), most used first.

    Each suggestion carries the active category it was used with most often (ties: the most
    recently used, then the category order). Deleted transactions do not count; archived
    categories are never suggested.
    """
    require_role(ctx, Role.VIEWER)
    wanted = _direction(direction)
    key = description_key(prefix)
    if not key or limit <= 0:
        return []
    with factory() as session:
        repository = TransactionRepository(session, ctx)
        keys = repository.description_keys(direction=wanted.value, key_prefix=key, limit=limit)
        details = repository.description_details(direction=wanted.value, keys=keys)
        order = {
            c.id: c.sort_order
            for c in CategoryRepository(session, ctx).ordered(wanted.value)
            if c.archived_at is None
        }
    return _suggestions(keys, details, order)


def _rank(uses: int, last: date) -> tuple[int, int]:
    """Sort key: most used first, then most recently used."""
    return (-uses, -last.toordinal())


def _suggestions(
    keys: list[str], details: list[DescriptionMatch], order: Mapping[UUID, int]
) -> list[Suggestion]:
    """One suggestion per key, in the ranked order of ``keys``."""
    spellings: dict[str, dict[str, tuple[int, date]]] = {key: {} for key in keys}
    categories: dict[str, dict[UUID, tuple[int, date]]] = {key: {} for key in keys}
    for match in details:
        uses, last = spellings[match.description_key].get(match.description, (0, date.min))
        spellings[match.description_key][match.description] = (
            uses + match.uses,
            max(last, match.last_date),
        )
        if match.category_id in order:  # active categories of the direction only
            used = categories[match.description_key]
            uses, last = used.get(match.category_id, (0, date.min))
            used[match.category_id] = (uses + match.uses, max(last, match.last_date))
    suggestions: list[Suggestion] = []
    for key in keys:
        texts, used = spellings[key], categories[key]
        if not texts:
            continue
        spelling = min(texts, key=lambda text: (_rank(*texts[text]), text))
        category = min(used, key=lambda cid: (_rank(*used[cid]), order[cid])) if used else None
        suggestions.append(Suggestion(spelling, category))
    return suggestions


# Writes (editors and owners)


def create_transaction(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    month: YearMonth,
    direction: str,
    day: str,
    amount: str,
    description: str,
    category_id: UUID | None,
    locale: str,
) -> TransactionView:
    """Add a transaction. ``day`` is a day of ``month`` or a full date (any month)."""
    require_role(ctx, Role.EDITOR)
    wanted = _direction(direction)
    entry = _entry(
        month=month,
        day=day,
        amount=amount,
        description=description,
        category_id=category_id,
        locale=locale,
        currency=ctx.workspace.base_currency,
    )
    assert category_id is not None  # checked by _entry

    def write(session: Session) -> TransactionView:
        require_role_now(session, ctx, Role.EDITOR)
        ensure_date_open(session, ctx, entry.day)  # takes the month-write lock first
        currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
        minor = _minor(entry.amount, currency)
        category = _category(session, ctx, category_id, wanted.value)
        row = TransactionRepository(session, ctx).create(
            direction=wanted.value,
            day=entry.day,
            amount_minor=minor,
            currency=currency,
            category_id=category.id,
            description=entry.description,
            description_key=entry.key,
            source=Source.MANUAL.value,
            created_by_user_id=ctx.user.id,
        )
        _audit(session, ctx, "transaction.created", row, category)
        return _view(row, category)

    return run_immediate(factory, write)


def update_transaction(
    factory: sessionmaker[Session],
    ctx: Context,
    transaction_id: UUID,
    *,
    month: YearMonth,
    day: str,
    amount: str,
    description: str,
    category_id: UUID | None,
    locale: str,
) -> TransactionView:
    """Change a transaction's date, amount, description or category (not its direction).

    Both the stored month and, when the date moves, the new month must be open. An archived
    category may stay on the row but cannot be chosen anew.
    """
    require_role(ctx, Role.EDITOR)
    entry = _entry(
        month=month,
        day=day,
        amount=amount,
        description=description,
        category_id=category_id,
        locale=locale,
        currency=ctx.workspace.base_currency,
    )
    assert category_id is not None  # checked by _entry

    def write(session: Session) -> TransactionView:
        require_role_now(session, ctx, Role.EDITOR)
        # The lock comes before reading the row whose date the guard checks (F2.5).
        lock_month_writes(session, ctx)
        repository = TransactionRepository(session, ctx)
        row = _live_row(repository, transaction_id, lock=True)
        ensure_date_open(session, ctx, row.date)
        if YearMonth.from_date(entry.day) != YearMonth.from_date(row.date):
            ensure_date_open(session, ctx, entry.day)
        minor = _minor(entry.amount, row.currency)
        category = _category(session, ctx, category_id, row.direction, keep=row.category_id)
        previous_date, previous_minor = row.date, row.amount_minor
        changes: dict[str, object] = {
            name: value
            for name, value in (
                ("date", entry.day),
                ("amount_minor", minor),
                ("description", entry.description),
                ("category_id", category.id),
            )
            if getattr(row, name) != value
        }
        if not changes:
            return _view(row, category)
        if "description" in changes:
            changes["description_key"] = entry.key
        repository.update(row.id, **changes)
        extra = {
            "fields": ",".join(
                sorted(
                    {"amount_minor": "amount", "category_id": "category"}.get(name, name)
                    for name in changes
                    if name != "description_key"
                )
            )
        }
        if "date" in changes:
            extra["previous_date"] = previous_date.isoformat()
        if "amount_minor" in changes:
            extra["previous_amount"] = _amount_text(previous_minor, row.currency)
        _audit(session, ctx, "transaction.updated", row, category, extra)
        return _view(row, category)

    return run_immediate(factory, write)


def delete_transaction(
    factory: sessionmaker[Session], ctx: Context, transaction_id: UUID
) -> DeletedTransaction:
    """Soft-delete a transaction; it can be restored within ``UNDO_WINDOW``.

    Deleting an already deleted row changes nothing and records nothing.
    """
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> DeletedTransaction:
        require_role_now(session, ctx, Role.EDITOR)
        lock_month_writes(session, ctx)
        repository = TransactionRepository(session, ctx)
        row = repository.get_for_update(transaction_id)  # serialises writes to this row
        if row is None:
            raise NotFound
        if row.deleted_at is None:
            ensure_date_open(session, ctx, row.date)
            repository.update(row.id, deleted_at=ctx.clock.now())
            category = CategoryRepository(session, ctx).get_or_raise(row.category_id)
            _audit(session, ctx, "transaction.deleted", row, category)
        assert row.deleted_at is not None
        return DeletedTransaction(
            id=row.id,
            month=YearMonth.from_date(row.date),
            undo_until=row.deleted_at + UNDO_WINDOW,
        )

    return run_immediate(factory, write)


def undo_delete(
    factory: sessionmaker[Session], ctx: Context, transaction_id: UUID
) -> TransactionView:
    """Restore a soft-deleted transaction within ``UNDO_WINDOW`` of its deletion.

    Any editor or owner may undo. After the window: ``UndoExpired``. If the month was closed
    meanwhile: ``MonthClosed`` (the row stays deleted). Undoing a live row changes nothing.
    """
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> TransactionView:
        require_role_now(session, ctx, Role.EDITOR)
        lock_month_writes(session, ctx)
        repository = TransactionRepository(session, ctx)
        row = repository.get_for_update(transaction_id)  # serialises writes to this row
        if row is None:
            raise NotFound
        category = CategoryRepository(session, ctx).get_or_raise(row.category_id)
        if row.deleted_at is None:
            return _view(row, category)
        if ctx.clock.now() - row.deleted_at > UNDO_WINDOW:
            raise UndoExpired
        ensure_date_open(session, ctx, row.date)
        repository.update(row.id, deleted_at=None)
        _audit(session, ctx, "transaction.restored", row, category)
        return _view(row, category)

    return run_immediate(factory, write)


def pending_undo(
    factory: sessionmaker[Session], ctx: Context, transaction_id: UUID
) -> DeletedTransaction | None:
    """The deletion of ``transaction_id`` while it can still be undone, else ``None``.

    ``None`` for a live row, a deletion older than ``UNDO_WINDOW``, an unknown id or another
    workspace's row. The month page offers Undo (without JavaScript) only for these.
    """
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        row = TransactionRepository(session, ctx).get(transaction_id)
        if row is None or row.deleted_at is None:
            return None
        if ctx.clock.now() - row.deleted_at > UNDO_WINDOW:
            return None
        return DeletedTransaction(
            id=row.id,
            month=YearMonth.from_date(row.date),
            undo_until=row.deleted_at + UNDO_WINDOW,
        )


# Seams used by other services


def category_usage(session: Session, ctx: Context) -> dict[UUID, CategoryUsage]:
    """The transactions' part of ``services.categories.USAGE_SOURCES``."""
    repository = TransactionRepository(session, ctx)
    months = repository.active_months_by_category()
    return {
        category_id: CategoryUsage(
            references=counts.references,
            active_transactions=counts.active,
            active_months=months.get(category_id, frozenset()),
        )
        for category_id, counts in repository.counts_by_category().items()
    }


def workspace_has_transactions(session: Session, ctx: Context) -> bool:
    """Part of ``services.workspaces.BASE_CURRENCY_LOCKS``: deleted rows count too."""
    return TransactionRepository(session, ctx).any_exists()
