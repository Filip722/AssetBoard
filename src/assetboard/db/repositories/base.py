"""``ScopedRepository``: the only way to read or write workspace-owned rows (F1.2, ADR-0003).

Every query adds ``workspace_id = ctx.workspace.id``; ``add`` sets it. A row of another workspace
is indistinguishable from a missing row: ``get`` returns ``None`` and the other methods raise
``NotFound``. Nothing public accepts a statement or a workspace id. Subclasses build extra
queries on ``_scoped_select()``; each extra public method needs an entry in the tenant-isolation
coverage registry (``tests/tenant/coverage.py``).

Updates go through ``update`` (or by changing attributes of rows returned by ``get``); a row's
``workspace_id`` can never change (see ``reject_forbidden_changes``). Bulk ``update()`` and
``delete()`` statements are not offered.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal, Protocol
from uuid import UUID

from sqlalchemy import Select, inspect, select
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy.orm.attributes import instance_state

from assetboard.db.base import WorkspaceOwned

Operation = Literal["update", "delete"]
IMMUTABLE_COLUMNS = frozenset({"id", "workspace_id"})


class _HasId(Protocol):
    @property
    def id(self) -> UUID: ...


class WorkspaceScope(Protocol):
    """What a repository needs from ``services.context.Context``: the current workspace."""

    @property
    def workspace(self) -> _HasId: ...


class RepositoryError(Exception):
    pass


class NotFound(RepositoryError):
    """No such row in the current workspace (rows of other workspaces look exactly like this)."""


class ForeignWorkspace(RepositoryError):
    """``add`` was given a row that already belongs to another workspace."""


class UnsupportedOperation(RepositoryError):
    """The repository does not offer this operation (for example, audit events are append-only)."""


_REGISTRY: dict[type[Any], type[ScopedRepository[Any]]] = {}


class ScopedRepository[T: WorkspaceOwned]:
    model: ClassVar[type[Any]]
    unsupported: ClassVar[frozenset[Operation]] = frozenset()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        model = cls.__dict__.get("model")
        if model is None or not issubclass(model, WorkspaceOwned):
            raise TypeError(f"{cls.__name__} must set `model` to a WorkspaceOwned class")
        existing = _REGISTRY.get(model)
        if existing is not None and existing is not cls:
            raise TypeError(f"{model.__name__} already has a repository: {existing.__name__}")
        _REGISTRY[model] = cls

    def __init__(self, session: Session, ctx: WorkspaceScope) -> None:
        self._session = session
        self._workspace_id: UUID = ctx.workspace.id

    def _scoped_select(self) -> Select[T]:
        return select(self.model).where(self.model.workspace_id == self._workspace_id)

    def _require(self, operation: Operation) -> None:
        if operation in self.unsupported:
            raise UnsupportedOperation(f"{type(self).__name__} does not support {operation}")

    def _check_type(self, obj: object) -> None:
        if type(obj) is not self.model:
            raise TypeError(f"{type(self).__name__} handles {self.model.__name__} rows only")

    def get(self, row_id: UUID) -> T | None:
        statement = self._scoped_select().where(self.model.id == row_id)
        return self._session.scalars(statement).one_or_none()

    def get_or_raise(self, row_id: UUID) -> T:
        row = self.get(row_id)
        if row is None:
            raise NotFound
        return row

    def list(self) -> list[T]:
        return list(self._session.scalars(self._scoped_select().order_by(self.model.id)))

    def add(self, obj: T) -> T:
        self._check_type(obj)
        current = getattr(obj, "workspace_id", None)
        if current is not None and current != self._workspace_id:
            raise ForeignWorkspace
        setattr(obj, "workspace_id", self._workspace_id)  # noqa: B010 - T is only a marker type
        self._session.add(obj)
        self._session.flush()
        return obj

    def update(self, row_id: UUID, **values: object) -> T:
        self._require("update")
        columns = set(inspect(self.model).columns.keys())
        invalid = set(values) - columns | (set(values) & IMMUTABLE_COLUMNS)
        if invalid:
            raise ValueError(f"cannot update {', '.join(sorted(invalid))}")
        row = self.get_or_raise(row_id)
        for name, value in values.items():
            setattr(row, name, value)
        self._session.flush()
        return row

    def delete(self, target: T | UUID) -> None:
        self._require("delete")
        if isinstance(target, UUID):
            row = self.get_or_raise(target)
        else:
            self._check_type(target)
            if getattr(target, "workspace_id", None) != self._workspace_id:
                raise NotFound
            # Re-read through the scope: the object must be this workspace's persistent row.
            identity = instance_state(target).identity
            if identity is None or self.get(identity[0]) is not target:
                raise NotFound
            row = target
        self._session.delete(row)
        self._session.flush()


# Discovery (used by the tenant-isolation test; takes the declarative base as a parameter)


def mapped_classes(base: type[DeclarativeBase]) -> set[type[Any]]:
    return {mapper.class_ for mapper in base.registry.mappers}


def models_with_workspace_column(base: type[DeclarativeBase]) -> set[type[Any]]:
    return {mapper.class_ for mapper in base.registry.mappers if "workspace_id" in mapper.columns}


def models_marked_workspace_owned(base: type[DeclarativeBase]) -> set[type[Any]]:
    return {cls for cls in mapped_classes(base) if issubclass(cls, WorkspaceOwned)}


def repositories_for(base: type[DeclarativeBase]) -> dict[type[Any], type[ScopedRepository[Any]]]:
    """Registered repositories whose model is mapped on ``base``."""
    mapped = mapped_classes(base)
    return {model: repo for model, repo in _REGISTRY.items() if model in mapped}
