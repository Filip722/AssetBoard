"""Reusable checks for the tenant-isolation test.

They take the declarative base, models and repositories as parameters, so the self-tests run
the very same functions against a scratch base with deliberately broken models.
"""

from __future__ import annotations

import importlib
import inspect as pyinspect
import pkgutil
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import inspect, select
from sqlalchemy.orm import DeclarativeBase, Session

import assetboard.db.models
import assetboard.db.repositories
from assetboard.clock import Clock
from assetboard.db.base import Base, WorkspaceOwned
from assetboard.db.repositories.base import (
    NotFound,
    ScopedRepository,
    UnsupportedOperation,
    models_marked_workspace_owned,
    models_with_workspace_column,
)
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef

ARCHITECTURE_HINT = "see docs/architecture.md, 'How to add a new workspace-owned model'"


def import_all_models() -> None:
    """Import every submodule of ``assetboard.db.models`` and ``assetboard.db.repositories``.

    Discovery must not depend on what some other import happened to load: a model module that
    nothing imports yet is still found, and every repository is registered.
    """
    for package in (assetboard.db.models, assetboard.db.repositories):
        for module in pkgutil.walk_packages(package.__path__, f"{package.__name__}."):
            importlib.import_module(module.name)


def owned_models(base: type[DeclarativeBase] = Base) -> list[type[Any]]:
    """Workspace-owned models of ``base`` (all modules imported first), sorted by name."""
    import_all_models()
    return sorted(models_with_workspace_column(base), key=lambda model: model.__name__)


def discovery_problems(
    base: type[DeclarativeBase],
    repositories: Mapping[type[Any], object],
    cases: Mapping[type[Any], object],
) -> list[str]:
    """Everything wrong with how ``base``'s workspace-owned models are registered.

    The marker and the column must agree, every workspace-owned model needs a repository and a
    tenant factory, and neither registry may hold anything else.
    """
    import_all_models()
    by_column = models_with_workspace_column(base)
    by_marker = models_marked_workspace_owned(base)
    problems: list[str] = []
    for model in sorted(by_column - by_marker, key=lambda m: m.__name__):
        problems.append(f"{model.__name__} has workspace_id but not the WorkspaceOwned marker")
    for model in sorted(by_marker - by_column, key=lambda m: m.__name__):
        problems.append(f"{model.__name__} is WorkspaceOwned but has no workspace_id column")
    for model in sorted(by_column | by_marker, key=lambda m: m.__name__):
        if model not in repositories:
            problems.append(f"{model.__name__} has no repository; {ARCHITECTURE_HINT}")
        if model not in cases:
            problems.append(f"{model.__name__} has no tenant factory; {ARCHITECTURE_HINT}")
    for model in sorted(set(repositories) - by_column, key=lambda m: m.__name__):
        problems.append(f"repository registered for {model.__name__}, which is not workspace-owned")
    for model in sorted(set(cases) - by_column, key=lambda m: m.__name__):
        problems.append(f"tenant factory for {model.__name__}, which is not workspace-owned")
    return problems


def relationships_into_owned_models(base: type[DeclarativeBase]) -> list[str]:
    """ORM relationships from a model that is not workspace-owned into one that is.

    Such a relationship (``Workspace.members``, ``User.audit_events``) loads workspace rows
    without the repository's scope, so it is not allowed.
    """
    import_all_models()
    problems: list[str] = []
    for mapper in sorted(base.registry.mappers, key=lambda m: m.class_.__name__):
        if issubclass(mapper.class_, WorkspaceOwned):
            continue
        for relationship in mapper.relationships:
            target = relationship.mapper.class_
            if issubclass(target, WorkspaceOwned):
                problems.append(f"{mapper.class_.__name__}.{relationship.key} -> {target.__name__}")
    return problems


def context_for(workspace_id: UUID, clock: Clock, role: Role = Role.OWNER) -> Context:
    return Context(
        user=UserRef(
            id=workspace_id,  # any id: repositories scope by workspace only
            email="synthetic@example.com",
            display_name="Synthetic",
            locale="en",
            is_instance_admin=False,
        ),
        workspace=WorkspaceRef(
            id=workspace_id,
            name="Synthetic",
            base_currency="EUR",
            locale="en",
            timezone="UTC",
            country_code=None,
        ),
        role=role,
        clock=clock,
    )


def raw_row(session: Session, model: type[Any], row_id: object) -> dict[str, object] | None:
    """Every column of a row, read straight from the table (no ORM identity map)."""
    table = inspect(model).local_table
    row = session.connection().execute(select(table).where(table.c.id == row_id)).one_or_none()
    return dict(row._mapping) if row is not None else None


def _expect(error: type[Exception], call: Callable[[], object], message: str) -> None:
    """Like ``pytest.raises``, but a missing exception is an ``AssertionError`` with ``message``."""
    try:
        call()
    except error:
        return
    raise AssertionError(message)


def check_isolation(
    session: Session,
    model: type[Any],
    repository: type[ScopedRepository[Any]],
    factory: Callable[[Session, UUID], Any],
    update_values: Mapping[str, object],
    ctx_a: Context,
    ctx_b: Context,
) -> None:
    """User B can never read, update or delete user A's rows, and vice versa.

    Positive controls run first (each side can read, list, update and delete its own rows), so a
    repository that returns nothing at all cannot pass. After the negative checks each foreign
    row is compared column by column with its state before. Every failure is an
    ``AssertionError`` whose message names the model and the operation.
    """
    for owner, other in ((ctx_a, ctx_b), (ctx_b, ctx_a)):
        _check_direction(session, model, repository, factory, update_values, owner, other)


def _check_direction(
    session: Session,
    model: type[Any],
    repository: type[ScopedRepository[Any]],
    factory: Callable[[Session, UUID], Any],
    update_values: Mapping[str, object],
    owner: Context,
    other: Context,
) -> None:
    name = model.__name__
    unsupported = repository.unsupported
    mine = repository(session, owner)
    theirs = repository(session, other)
    own_row = mine.add(factory(session, owner.workspace.id))
    their_row = theirs.add(factory(session, other.workspace.id))

    # Positive control: the owner sees and changes its own rows.
    control = f"{name}: positive control failed:"
    assert mine.get(own_row.id) is own_row, f"{control} get of an own row"
    assert mine.get_or_raise(own_row.id) is own_row, f"{control} get_or_raise of an own row"
    listed = {row.id for row in mine.list()}
    assert own_row.id in listed, f"{control} list misses an own row"
    assert their_row.id not in listed, f"{name}: could read a row of another workspace through list"
    spare = mine.add(factory(session, owner.workspace.id))
    if "update" in unsupported:
        _expect(
            UnsupportedOperation,
            lambda: mine.update(spare.id, **update_values),
            f"{name}: update should be unsupported",
        )
    else:
        assert update_values, f"{name} needs update_values in TENANT_CASES"
        mine.update(spare.id, **update_values)
        for column, value in update_values.items():
            assert getattr(spare, column) == value, f"{control} update of an own row"
    if "delete" in unsupported:
        _expect(
            UnsupportedOperation,
            lambda: mine.delete(spare.id),
            f"{name}: delete should be unsupported",
        )
    else:
        mine.delete(spare.id)
        assert raw_row(session, model, spare.id) is None, f"{control} delete of an own row"

    # Negative checks: the other workspace cannot reach the owner's row.
    before = raw_row(session, model, own_row.id)
    assert before is not None and before["workspace_id"] == owner.workspace.id
    leak = f"{name}: could read a row of another workspace through"
    assert theirs.get(own_row.id) is None, f"{leak} get"
    _expect(NotFound, lambda: theirs.get_or_raise(own_row.id), f"{leak} get_or_raise")
    assert own_row.id not in {row.id for row in theirs.list()}, f"{leak} list"
    update_error = UnsupportedOperation if "update" in unsupported else NotFound
    delete_error = UnsupportedOperation if "delete" in unsupported else NotFound
    reach = f"{name}: could change a row of another workspace through"
    _expect(update_error, lambda: theirs.update(own_row.id, **update_values), f"{reach} update")
    _expect(delete_error, lambda: theirs.delete(own_row.id), f"{reach} delete by id")
    _expect(delete_error, lambda: theirs.delete(own_row), f"{reach} delete by object")
    session.flush()
    session.expire_all()
    assert raw_row(session, model, own_row.id) == before, (
        f"{name}: a row of another workspace changed after the foreign attempts"
    )


def public_methods(cls: type[Any], stop: type[Any] | None = None) -> set[str]:
    """Public callables defined on ``cls`` and its bases, up to (excluding) ``stop``'s bases."""
    names: set[str] = set()
    for klass in cls.__mro__:
        if klass is object or (stop is not None and klass in stop.__mro__[1:]):
            continue
        for name, value in vars(klass).items():
            if not name.startswith("_") and pyinspect.isfunction(value):
                names.add(name)
    return names


def uncovered_methods(
    repositories: Iterable[type[ScopedRepository[Any]]],
    base_coverage: Mapping[str, str],
    extra_coverage: Mapping[tuple[str, str], str],
) -> list[str]:
    missing: list[str] = []
    base_methods = public_methods(ScopedRepository)
    for repository in repositories:
        for name in sorted(public_methods(repository, stop=ScopedRepository)):
            if name in base_methods and name in base_coverage:
                continue
            if (repository.__name__, name) not in extra_coverage:
                missing.append(f"{repository.__name__}.{name}")
    return missing


def non_composite_workspace_fks(base: type[DeclarativeBase]) -> list[str]:
    """Foreign keys between workspace-owned tables that do not include ``workspace_id``.

    A single-column FK from one workspace-owned table to another would let a row point at a
    parent in another workspace; the FK must be ``(workspace_id, x_id) -> (workspace_id, id)``.
    """
    owned_tables = {inspect(model).local_table for model in models_marked_workspace_owned(base)}
    problems: list[str] = []
    for table in sorted(owned_tables, key=lambda t: t.name):
        for constraint in table.foreign_key_constraints:
            if constraint.referred_table not in owned_tables:
                continue
            local = {column.name for column in constraint.columns}
            remote = {element.column.name for element in constraint.elements}
            if "workspace_id" not in local or "workspace_id" not in remote:
                problems.append(
                    f"{table.name}({', '.join(sorted(local))}) -> {constraint.referred_table.name}"
                )
    return problems
