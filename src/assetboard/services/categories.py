"""Categories (F2.1): list, create, edit, reorder, archive, restore, delete and the seed set.

Everyone in the workspace may read categories; writes need ``editor``. Each write runs in one
``BEGIN IMMEDIATE`` transaction that re-reads the actor's role (``require_role_now``), so a
Context built before a demotion cannot write.

**Usage seam.** Whether a category is "in use" comes from ``USAGE_SOURCES``: callables that
return, per category id, a ``CategoryUsage`` (``services.category_usage``). The list is static;
each feature adds its source here:

* F2.2 transactions: ``references`` counts every transaction row, soft-deleted ones included
  (they still point at the category, and undo must keep working); ``active_transactions``
  counts the non-deleted ones (any date) and is shown as "affected" when the treatment changes;
  ``active_months`` are the months with non-deleted rows.
* F2.3 budget lines: ``references`` counts the lines (any month; removed lines no longer
  exist), ``active_months`` the months they plan. M3 scheduled items: ``references``.

A category with ``references > 0`` cannot change its kind, and deleting it archives it instead.
A treatment change is allowed; the edit form lists the **closed** months among
``active_months``, whose totals the change alters (owner decision, F2.2).

**Concurrency.** Writes that read usage or change a category's kind or archived state (update,
archive, restore, delete) take the exclusive workspace lock (``CurrentWorkspaceRepository.lock``)
right after the role check, before reading anything else. Transaction writes take the shared one
before reading the category, so on PostgreSQL a transaction can never land on a category that
just changed kind or was archived, and a kind change always sees every transaction.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.ledger import Category
from assetboard.db.repositories import (
    AuditEventRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    MonthStateRepository,
)
from assetboard.db.repositories.base import WorkspaceScope
from assetboard.domain.categories import (
    SEED,
    Direction,
    InvalidCategoryName,
    InvalidColor,
    Kind,
    Treatment,
    clean_color,
    clean_name,
    move,
    name_key,
)
from assetboard.domain.months import YearMonth
from assetboard.i18n import gettext
from assetboard.services.budget import category_usage as budget_line_usage
from assetboard.services.category_usage import CategoryUsage, UsageSource
from assetboard.services.context import Context, Role, require_role, require_role_now
from assetboard.services.transactions import category_usage as transaction_usage

__all__ = ["USAGE_SOURCES", "CategoryUsage", "UsageSource"]

# Every feature whose rows reference categories (see the module docstring).
USAGE_SOURCES: list[UsageSource] = [transaction_usage, budget_line_usage]


class CategoryError(Exception):
    code = "invalid"
    field = ""


class InvalidCategory(CategoryError):
    def __init__(self, field: str, code: str) -> None:
        super().__init__(code)
        self.field = field
        self.code = code


class DuplicateName(CategoryError):
    """Another non-archived category of the same kind has this name (ignoring case)."""

    code = "duplicate_name"
    field = "name"


class KindChangeBlocked(CategoryError):
    """The category is in use, so its kind cannot change."""

    code = "kind_in_use"
    field = "kind"


class CategoryArchived(CategoryError):
    """Archived categories can only be restored (or deleted)."""

    code = "archived"


class AlreadySeeded(CategoryError):
    """The suggested set is offered only to a workspace without any category."""

    code = "already_seeded"


@dataclass(frozen=True, slots=True)
class CategoryView:
    id: UUID
    name: str
    kind: str
    treatment: str
    color: str
    sort_order: int
    archived: bool
    references: int
    active_transactions: int
    # Closed months whose totals a treatment change alters (oldest first).
    closed_months_affected: tuple[YearMonth, ...] = ()

    @property
    def in_use(self) -> bool:
        return self.references > 0


@dataclass(frozen=True, slots=True)
class CategoryList:
    expense: tuple[CategoryView, ...]
    income: tuple[CategoryView, ...]
    archived: tuple[CategoryView, ...]

    @property
    def can_seed(self) -> bool:
        return not (self.expense or self.income or self.archived)


DeleteOutcome = Literal["deleted", "archived"]
DIRECTIONS: frozenset[str] = frozenset({"up", "down"})


def usage_by_category(
    session: Session, ctx: Context, sources: Iterable[UsageSource] | None = None
) -> dict[UUID, CategoryUsage]:
    """The summed usage of every category that some source reports."""
    totals: dict[UUID, CategoryUsage] = {}
    for source in USAGE_SOURCES if sources is None else sources:
        for category_id, usage in source(session, ctx).items():
            totals[category_id] = totals.get(category_id, CategoryUsage()) + usage
    return totals


def _view(
    category: Category,
    usage: Mapping[UUID, CategoryUsage],
    closed: frozenset[YearMonth] = frozenset(),
) -> CategoryView:
    used = usage.get(category.id, CategoryUsage())
    return CategoryView(
        id=category.id,
        name=category.name,
        kind=category.kind,
        treatment=category.treatment,
        color=category.color,
        sort_order=category.sort_order,
        archived=category.archived_at is not None,
        references=used.references,
        active_transactions=used.active_transactions,
        closed_months_affected=tuple(sorted(used.active_months & closed)),
    )


def _lock_categories(session: Session, ctx: Context) -> None:
    """The exclusive workspace lock, before any read (see "Concurrency" above)."""
    CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)


def _closed_months(session: Session, ctx: Context) -> frozenset[YearMonth]:
    return MonthStateRepository(session, ctx).closed_months()


def _kind(value: str) -> Kind:
    try:
        return Kind(value)
    except ValueError:
        raise InvalidCategory("kind", "invalid_kind") from None


def _treatment(value: str) -> Treatment:
    try:
        return Treatment(value)
    except ValueError:
        raise InvalidCategory("treatment", "invalid_treatment") from None


def _name(value: str) -> str:
    try:
        return clean_name(value)
    except InvalidCategoryName:
        raise InvalidCategory("name", "invalid_name") from None


def _color(value: str) -> str:
    try:
        return clean_color(value)
    except InvalidColor:
        raise InvalidCategory("color", "invalid_color") from None


def _active(categories: Sequence[Category]) -> list[Category]:
    return [category for category in categories if category.archived_at is None]


def _next_sort_order(repository: CategoryRepository, kind: str) -> int:
    orders = [category.sort_order for category in _active(repository.ordered(kind))]
    return max(orders) + 1 if orders else 0


def _ensure_unique(
    repository: CategoryRepository, *, kind: str, key: str, own_id: UUID | None = None
) -> None:
    clash = repository.active_by_name_key(kind=kind, name_key=key)
    if clash is not None and clash.id != own_id:
        raise DuplicateName


def _write[T](factory: sessionmaker[Session], operation: Callable[[Session], T]) -> T:
    """Run a write; a concurrent duplicate that passed the pre-check fails at the index."""
    try:
        return run_immediate(factory, operation)
    except IntegrityError as exc:
        if "name_key" in str(exc.orig) or "uq_categories_active_name" in str(exc.orig):
            raise DuplicateName from exc
        raise


def _audit(
    session: Session,
    ctx: Context,
    action: str,
    category_id: UUID,
    data: Mapping[str, str | int | bool],
) -> None:
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action=action,
        entity_type="category",
        entity_id=str(category_id),
        ts=ctx.clock.now(),
        data=data,
    )


# Reads (every member)


def list_categories(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    usage_sources: Iterable[UsageSource] | None = None,
) -> CategoryList:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        usage = usage_by_category(session, ctx, usage_sources)
        closed = _closed_months(session, ctx)
        views = [_view(c, usage, closed) for c in CategoryRepository(session, ctx).ordered()]
    return CategoryList(
        expense=tuple(v for v in views if not v.archived and v.kind == Kind.EXPENSE),
        income=tuple(v for v in views if not v.archived and v.kind == Kind.INCOME),
        archived=tuple(v for v in views if v.archived),
    )


def get_category(
    factory: sessionmaker[Session],
    ctx: Context,
    category_id: UUID,
    *,
    usage_sources: Iterable[UsageSource] | None = None,
) -> CategoryView:
    """One category; ``NotFound`` for an unknown id or another workspace's category."""
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        category = CategoryRepository(session, ctx).get_or_raise(category_id)
        usage = usage_by_category(session, ctx, usage_sources)
        return _view(category, usage, _closed_months(session, ctx))


# Writes (editors and owners)


def create_category(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    name: str,
    kind: str,
    treatment: str,
    color: str,
) -> CategoryView:
    require_role(ctx, Role.EDITOR)
    clean = _name(name)
    wanted_kind, wanted_treatment, wanted_color = _kind(kind), _treatment(treatment), _color(color)
    key = name_key(clean)

    def write(session: Session) -> CategoryView:
        require_role_now(session, ctx, Role.EDITOR)
        repository = CategoryRepository(session, ctx)
        _ensure_unique(repository, kind=wanted_kind, key=key)
        category = repository.create(
            name=clean,
            name_key=key,
            kind=wanted_kind.value,
            treatment=wanted_treatment.value,
            color=wanted_color,
            sort_order=_next_sort_order(repository, wanted_kind),
        )
        _audit(
            session,
            ctx,
            "category.created",
            category.id,
            {"name": clean, "kind": wanted_kind.value, "treatment": wanted_treatment.value},
        )
        return _view(category, {})

    return _write(factory, write)


def update_category(
    factory: sessionmaker[Session],
    ctx: Context,
    category_id: UUID,
    *,
    name: str,
    kind: str,
    treatment: str,
    color: str,
    usage_sources: Iterable[UsageSource] | None = None,
) -> CategoryView:
    """Change a category. The kind cannot change while the category is in use."""
    require_role(ctx, Role.EDITOR)
    clean = _name(name)
    wanted_kind, wanted_treatment, wanted_color = _kind(kind), _treatment(treatment), _color(color)
    key = name_key(clean)

    def write(session: Session) -> CategoryView:
        require_role_now(session, ctx, Role.EDITOR)
        _lock_categories(session, ctx)
        repository = CategoryRepository(session, ctx)
        category = repository.get_or_raise(category_id)
        if category.archived_at is not None:
            raise CategoryArchived
        usage = usage_by_category(session, ctx, usage_sources)
        closed = _closed_months(session, ctx)
        used = usage.get(category.id, CategoryUsage())
        kind_changes = category.kind != wanted_kind.value
        if kind_changes and used.references > 0:
            raise KindChangeBlocked
        if kind_changes or category.name_key != key:
            _ensure_unique(repository, kind=wanted_kind, key=key, own_id=category.id)
        changes: dict[str, object] = {
            field: value
            for field, value in (
                ("name", clean),
                ("kind", wanted_kind.value),
                ("treatment", wanted_treatment.value),
                ("color", wanted_color),
            )
            if getattr(category, field) != value
        }
        if not changes:
            return _view(category, usage, closed)
        previous_treatment = category.treatment
        if "name" in changes:
            changes["name_key"] = key
        if kind_changes:
            # The category moves to the end of its new kind's list.
            changes["sort_order"] = _next_sort_order(repository, wanted_kind)
        repository.update(category.id, **changes)
        data: dict[str, str | int | bool] = {
            "name": clean,
            "fields": ",".join(sorted(f for f in changes if f not in {"name_key", "sort_order"})),
        }
        if "treatment" in changes:
            data |= {
                "previous_treatment": previous_treatment,
                "treatment": wanted_treatment.value,
                "affected_transactions": used.active_transactions,
                "closed_months_affected": len(used.active_months & closed),
            }
        _audit(session, ctx, "category.updated", category.id, data)
        return _view(category, usage, closed)

    return _write(factory, write)


def move_category(
    factory: sessionmaker[Session], ctx: Context, category_id: UUID, direction: str
) -> None:
    """Swap an active category with its neighbour in its kind's list (not audited)."""
    require_role(ctx, Role.EDITOR)
    if direction not in DIRECTIONS:
        raise InvalidCategory("direction", "invalid_direction")
    step: Direction = "up" if direction == "up" else "down"

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        repository = CategoryRepository(session, ctx)
        category = repository.get_or_raise(category_id)
        if category.archived_at is not None:
            raise CategoryArchived
        siblings = _active(repository.ordered(category.kind))
        for position, sibling in enumerate(move(siblings, category, step)):
            if sibling.sort_order != position:
                sibling.sort_order = position
        session.flush()

    run_immediate(factory, write)


def archive_category(
    factory: sessionmaker[Session],
    ctx: Context,
    category_id: UUID,
    *,
    usage_sources: Iterable[UsageSource] | None = None,
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _lock_categories(session, ctx)
        category = CategoryRepository(session, ctx).get_or_raise(category_id)
        if category.archived_at is None:
            used = usage_by_category(session, ctx, usage_sources).get(category.id)
            _archive(session, ctx, category, in_use=used is not None and used.references > 0)

    run_immediate(factory, write)


def _archive(session: Session, ctx: Context, category: Category, *, in_use: bool) -> None:
    category.archived_at = ctx.clock.now()
    session.flush()
    _audit(
        session, ctx, "category.archived", category.id, {"name": category.name, "in_use": in_use}
    )


def restore_category(factory: sessionmaker[Session], ctx: Context, category_id: UUID) -> None:
    """Bring an archived category back, at the end of its kind's list."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _lock_categories(session, ctx)
        repository = CategoryRepository(session, ctx)
        category = repository.get_or_raise(category_id)
        if category.archived_at is None:
            return
        _ensure_unique(repository, kind=category.kind, key=category.name_key)
        repository.update(
            category.id,
            archived_at=None,
            sort_order=_next_sort_order(repository, category.kind),
        )
        _audit(session, ctx, "category.restored", category.id, {"name": category.name})

    _write(factory, write)


def delete_category(
    factory: sessionmaker[Session],
    ctx: Context,
    category_id: UUID,
    *,
    usage_sources: Iterable[UsageSource] | None = None,
) -> DeleteOutcome:
    """Delete an unused category; a category in use is archived instead."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> DeleteOutcome:
        require_role_now(session, ctx, Role.EDITOR)
        _lock_categories(session, ctx)
        repository = CategoryRepository(session, ctx)
        category = repository.get_or_raise(category_id)
        used = usage_by_category(session, ctx, usage_sources).get(category.id, CategoryUsage())
        if used.references > 0:
            if category.archived_at is None:
                _archive(session, ctx, category, in_use=True)
            return "archived"
        name, kind = category.name, category.kind
        repository.delete(category)
        _audit(session, ctx, "category.deleted", category_id, {"name": name, "kind": kind})
        return "deleted"

    return run_immediate(factory, write)


def seed_into(
    session: Session,
    scope: WorkspaceScope,
    *,
    locale: str,
    actor_user_id: UUID | None,
    actor_type: Literal["user", "cli", "system"],
    now: datetime,
    automatic: bool,
) -> int:
    """Create the suggested categories in ``scope``'s workspace, named in ``locale``.

    The caller's transaction must have checked that the workspace has no category. Every new
    workspace gets the set automatically (``automatic=True``, pre-release A); the "Add
    suggested categories" button calls it for an older workspace without any category.
    Recorded as ``category.seeded`` in that workspace.
    """
    repository = CategoryRepository(session, scope)
    positions: dict[Kind, int] = {}
    for seed in SEED:
        name = clean_name(gettext(seed.name, locale))
        position = positions.get(seed.kind, 0)
        positions[seed.kind] = position + 1
        repository.create(
            name=name,
            name_key=name_key(name),
            kind=seed.kind.value,
            treatment=seed.treatment.value,
            color=seed.color,
            sort_order=position,
        )
    data: dict[str, str | int | bool] = {"count": len(SEED)}
    if automatic:
        data["automatic"] = True
    AuditEventRepository(session, scope).record(
        actor_user_id=actor_user_id,
        actor_type=actor_type,
        action="category.seeded",
        entity_type="workspace",
        entity_id=str(scope.workspace.id),
        ts=now,
        data=data,
    )
    return len(SEED)


def seed_categories(factory: sessionmaker[Session], ctx: Context, *, locale: str) -> int:
    """The suggested categories, named in ``locale``; only for a workspace without any."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> int:
        require_role_now(session, ctx, Role.EDITOR)
        if CategoryRepository(session, ctx).count() > 0:
            raise AlreadySeeded
        return seed_into(
            session,
            ctx,
            locale=locale,
            actor_user_id=ctx.user.id,
            actor_type="user",
            now=ctx.clock.now(),
            automatic=False,
        )

    try:
        return run_immediate(factory, write)
    except IntegrityError as exc:
        # A concurrent seed (PostgreSQL does not queue writers at BEGIN) won the race.
        raise AlreadySeeded from exc
