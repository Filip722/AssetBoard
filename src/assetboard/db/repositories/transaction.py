"""Transactions of the current workspace (F2.2)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import ColumnElement, Integer, case, cast, exists, extract, func, select

from assetboard.db.models.ledger import Transaction
from assetboard.db.repositories.base import ScopedRepository
from assetboard.domain.months import YearMonth


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
