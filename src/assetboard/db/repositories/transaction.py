"""Transactions of the current workspace (F2.2)."""

from __future__ import annotations

from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Integer,
    Select,
    and_,
    case,
    cast,
    exists,
    extract,
    func,
    select,
)
from sqlalchemy.sql.elements import UnaryExpression

from assetboard.db.models.ledger import Category, Transaction
from assetboard.db.repositories.base import ScopedRepository
from assetboard.domain.months import YearMonth
from assetboard.domain.transaction_search import SortOrder, TransactionFilter

# Entry order (M4's export order): date, then the order rows were entered.
CANONICAL_ORDER = (Transaction.date, Transaction.created_at, Transaction.id)
_SORTS: dict[SortOrder, tuple[UnaryExpression[Any], ...]] = {
    SortOrder.DATE_DESC: (
        Transaction.date.desc(),
        Transaction.created_at.desc(),
        Transaction.id.desc(),
    ),
    SortOrder.DATE_ASC: (
        Transaction.date.asc(),
        Transaction.created_at.asc(),
        Transaction.id.asc(),
    ),
    # amount_minor is a BIGINT (not a DecimalType), so SQL may order by it.
    SortOrder.AMOUNT_DESC: (
        Transaction.amount_minor.desc(),
        Transaction.date.desc(),
        Transaction.created_at.desc(),
        Transaction.id.desc(),
    ),
    SortOrder.AMOUNT_ASC: (
        Transaction.amount_minor.asc(),
        Transaction.date.asc(),
        Transaction.created_at.asc(),
        Transaction.id.asc(),
    ),
}


@dataclass(frozen=True, slots=True)
class CategoryCounts:
    references: int  # every row, soft-deleted ones included
    active: int  # rows that are not deleted


@dataclass(frozen=True, slots=True)
class DescriptionMatch:
    """One (description, category) pair among non-deleted rows, with its use count."""

    description_key: str
    description: str
    category_id: UUID
    uses: int
    last_date: date


class TransactionRepository(ScopedRepository[Transaction]):
    """Rows are soft-deleted (``deleted_at``), never removed: generic ``delete`` is unsupported.

    Queries for the month page and autocomplete skip soft-deleted rows; ``get`` returns them
    (the service decides what a deleted row means), and the usage counts include them because
    they still reference their category.
    """

    model = Transaction
    unsupported = frozenset({"delete"})

    def create(
        self,
        *,
        direction: str,
        day: date,
        amount_minor: int,
        currency: str,
        category_id: UUID,
        description: str,
        description_key: str,
        source: str,
        created_by_user_id: UUID | None,
    ) -> Transaction:
        return self.add(
            Transaction(
                direction=direction,
                date=day,
                amount_minor=amount_minor,
                currency=currency,
                category_id=category_id,
                description=description,
                description_key=description_key,
                source=source,
                created_by_user_id=created_by_user_id,
            )
        )

    def get_for_update(self, row_id: UUID) -> Transaction | None:
        """``get`` with a row lock (``FOR UPDATE`` on PostgreSQL) until the transaction ends.

        Serialises concurrent edits, deletes and undos of the same row. SQLite has no row locks;
        there ``BEGIN IMMEDIATE`` already serialises writers and the clause is left out.
        """
        statement = self._scoped_select().where(Transaction.id == row_id).with_for_update()
        return self._session.scalars(statement).one_or_none()

    def for_month(self, month: YearMonth, direction: str | None = None) -> list[Transaction]:
        """Non-deleted rows dated in ``month``, by date and then entry order."""
        statement = self._scoped_select().where(
            Transaction.deleted_at.is_(None),
            Transaction.date >= month.first_day,
            Transaction.date <= month.last_day,
        )
        if direction is not None:
            statement = statement.where(Transaction.direction == direction)
        statement = statement.order_by(Transaction.date, Transaction.created_at, Transaction.id)
        return list(self._session.scalars(statement))

    def counts_by_category(self) -> dict[UUID, CategoryCounts]:
        """Per category: all rows (deleted included) and non-deleted rows."""
        active = func.sum(case((Transaction.deleted_at.is_(None), 1), else_=0))
        statement = (
            select(Transaction.category_id, func.count(), active)
            .where(Transaction.workspace_id == self._workspace_id)
            .group_by(Transaction.category_id)
        )
        return {
            category_id: CategoryCounts(references=int(total), active=int(live or 0))
            for category_id, total, live in self._session.execute(statement)
        }

    def active_months_by_category(self) -> dict[UUID, frozenset[YearMonth]]:
        """Per category: the months that have non-deleted rows."""
        year = cast(extract("year", Transaction.date), Integer)
        month = cast(extract("month", Transaction.date), Integer)
        statement = (
            select(Transaction.category_id, year, month)
            .where(
                Transaction.workspace_id == self._workspace_id,
                Transaction.deleted_at.is_(None),
            )
            .group_by(Transaction.category_id, year, month)
        )
        months: dict[UUID, set[YearMonth]] = {}
        for category_id, y, m in self._session.execute(statement):
            months.setdefault(category_id, set()).add(YearMonth(int(y), int(m)))
        return {category_id: frozenset(found) for category_id, found in months.items()}

    def any_exists(self) -> bool:
        """Whether the workspace has any transaction, soft-deleted ones included."""
        statement = select(exists().where(Transaction.workspace_id == self._workspace_id))
        return bool(self._session.execute(statement).scalar_one())

    def _described(self, direction: str) -> tuple[ColumnElement[bool], ...]:
        return (
            Transaction.workspace_id == self._workspace_id,
            Transaction.deleted_at.is_(None),
            Transaction.direction == direction,
        )

    def description_keys(self, *, direction: str, key_prefix: str, limit: int) -> list[str]:
        """Description keys of non-deleted ``direction`` rows starting with ``key_prefix``.

        Ranked in SQL over all matching rows: most used first, then most recently dated, then
        the key. ``%`` and ``_`` in the prefix are literal. The caller passes a non-empty prefix.
        """
        uses = func.count()
        last = func.max(Transaction.date)
        statement = (
            select(Transaction.description_key)
            .where(
                *self._described(direction),
                Transaction.description_key.startswith(key_prefix, autoescape=True),
            )
            .group_by(Transaction.description_key)
            .order_by(uses.desc(), last.desc(), Transaction.description_key)
            .limit(limit)
        )
        return list(self._session.scalars(statement))

    def description_details(self, *, direction: str, keys: Sequence[str]) -> list[DescriptionMatch]:
        """For each of ``keys``: every (spelling, category) pair, its use count and last date."""
        if not keys:
            return []
        statement = (
            select(
                Transaction.description_key,
                Transaction.description,
                Transaction.category_id,
                func.count(),
                func.max(Transaction.date),
            )
            .where(*self._described(direction), Transaction.description_key.in_(list(keys)))
            .group_by(Transaction.description_key, Transaction.description, Transaction.category_id)
        )
        return [
            DescriptionMatch(key, description, category_id, int(count), last)
            for key, description, category_id, count, last in self._session.execute(statement)
        ]

    # Search (F2.6): non-deleted rows matching a ``TransactionFilter``

    def _matching(self, where: TransactionFilter) -> Select[Transaction]:
        statement = self._scoped_select().where(Transaction.deleted_at.is_(None))
        if where.date_from is not None:
            statement = statement.where(Transaction.date >= where.date_from)
        if where.date_to is not None:
            statement = statement.where(Transaction.date <= where.date_to)
        if where.direction is not None:
            statement = statement.where(Transaction.direction == where.direction)
        if where.category_ids:
            statement = statement.where(Transaction.category_id.in_(sorted(where.category_ids)))
        if where.text_key:
            statement = statement.where(
                Transaction.description_key.contains(where.text_key, autoescape=True)
            )
        if where.amount_min_minor is not None:
            statement = statement.where(Transaction.amount_minor >= where.amount_min_minor)
        if where.amount_max_minor is not None:
            statement = statement.where(Transaction.amount_minor <= where.amount_max_minor)
        return statement

    def _with_category(self, where: TransactionFilter) -> Select[Transaction, Category]:
        """Matching rows joined with their category (same workspace: composite foreign key)."""
        return (
            self._matching(where)
            .add_columns(Category)
            .join(
                Category,
                and_(
                    Category.workspace_id == Transaction.workspace_id,
                    Category.id == Transaction.category_id,
                ),
            )
        )

    def search(
        self, where: TransactionFilter, *, sort: SortOrder, limit: int, offset: int
    ) -> list[tuple[Transaction, Category]]:
        """One page of matching rows with their categories, in ``sort`` order (ties: entry
        order). The category comes from the same statement, so a row never lacks one."""
        statement = self._with_category(where).order_by(*_SORTS[sort]).limit(limit).offset(offset)
        return [(row, category) for row, category in self._session.execute(statement)]

    def count_matching(self, where: TransactionFilter) -> int:
        statement = select(func.count()).select_from(self._matching(where).subquery())
        return self._session.execute(statement).scalar_one()

    def iter_matching(
        self, where: TransactionFilter, *, batch: int = 1000
    ) -> Iterator[tuple[Transaction, Category]]:
        """Every matching row with its category in entry order, from one statement fetched
        ``batch`` rows at a time (the export)."""
        statement = self._with_category(where).order_by(*CANONICAL_ORDER)
        rows = self._session.execute(statement.execution_options(yield_per=batch))
        yield from ((row, category) for row, category in rows)

    def matching_for_update(
        self,
        *,
        where: TransactionFilter | None = None,
        ids: Collection[UUID] | None = None,
        limit: int | None = None,
        lock: bool = True,
    ) -> list[tuple[Transaction, Category]]:
        """The rows a bulk change targets, with their categories, in entry order.

        Either the rows matching ``where`` or the non-deleted rows among ``ids`` (exactly one).
        At most ``limit`` rows are loaded (the caller passes its cap + 1 to detect "too many").
        With ``lock`` the transaction rows (not the categories) are locked until the transaction
        ends: ``FOR UPDATE OF transactions`` on PostgreSQL; SQLite has no row locks and leaves
        the clause out (``BEGIN IMMEDIATE`` serialises writers there). A read-only preview
        passes ``lock=False``.
        """
        if (where is None) == (ids is None):
            raise ValueError("pass either where or ids")
        if where is not None:
            statement = self._with_category(where)
        else:
            assert ids is not None
            statement = self._with_category(TransactionFilter()).where(
                Transaction.id.in_(sorted(ids))
            )
        statement = statement.order_by(*CANONICAL_ORDER)
        if limit is not None:
            statement = statement.limit(limit)
        if lock:
            statement = statement.with_for_update(of=Transaction)
        return [(row, category) for row, category in self._session.execute(statement)]
