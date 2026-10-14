"""Categories of the current workspace (F2.1)."""

from __future__ import annotations

from sqlalchemy import func, select

from assetboard.db.models.ledger import Category
from assetboard.db.repositories.base import ScopedRepository


class CategoryRepository(ScopedRepository[Category]):
    """Generic ``update`` and ``delete`` are supported: a category that nothing references can
    be deleted (the service archives referenced ones instead)."""

    model = Category

    def create(
        self,
        *,
        name: str,
        name_key: str,
        kind: str,
        treatment: str,
        color: str,
        sort_order: int,
    ) -> Category:
        return self.add(
            Category(
                name=name,
                name_key=name_key,
                kind=kind,
                treatment=treatment,
                color=color,
                sort_order=sort_order,
            )
        )

    def ordered(self, kind: str | None = None) -> list[Category]:
        """Categories (of one kind, or all) in display order; archived ones included."""
        statement = self._scoped_select()
        if kind is not None:
            statement = statement.where(Category.kind == kind)
        statement = statement.order_by(
            Category.kind, Category.sort_order, Category.name_key, Category.id
        )
        return list(self._session.scalars(statement))

    def active_by_name_key(self, *, kind: str, name_key: str) -> Category | None:
        """The non-archived category of ``kind`` with this name key, if any."""
        statement = self._scoped_select().where(
            Category.kind == kind,
            Category.name_key == name_key,
            Category.archived_at.is_(None),
        )
        return self._session.scalars(statement).one_or_none()

    def count(self) -> int:
        """All categories of the workspace, archived ones included."""
        statement = (
            select(func.count())
            .select_from(Category)
            .where(Category.workspace_id == self._workspace_id)
        )
        return self._session.execute(statement).scalar_one()
