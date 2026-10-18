"""Planned values (F2.3): budget lines per (month, category), copying a plan, automatic copy.

Every member may read a month's plan; writes need ``editor``. A planned value is stored as a
``BudgetLine`` in positive minor units of the workspace's base currency (read from the workspace
row inside the write, after the lock). Saving zero or an empty value **removes** the line: there
is no soft delete and no undo for planned values. Saving the stored value changes nothing and
records nothing.

**Which categories.** A new line needs an active category that is not a transfer (transfers
are not counted in the month summary, F2.4): ``category_archived``, ``category_transfer``. An
existing line can be changed or removed whatever its category became, as a transaction keeps
an archived category.

**Copying.** ``copy_previous_plan`` copies the immediately previous month's lines into a month.
It never overwrites: categories that already have a line keep it. Lines whose category is now
archived or a transfer are skipped (and counted). ``auto_copy_on_open`` does the same when the
workspace setting ``auto_copy_plan`` is on, the month is the current one or later and open, the
actor is an editor or owner, and the month has never had a planned value
(``MonthState.plan_started_at``, set by the month's first planned-value write and never
cleared): a plan emptied on purpose is not filled again.

**Writes.** Each write runs in one ``BEGIN IMMEDIATE`` transaction (``run_immediate``) that
re-reads the actor's role (``require_role_now``), then takes the **exclusive** workspace lock
(on PostgreSQL; it orders planned-value writes against each other, so two upserts or copies
cannot both insert the same (month, category), and it also orders them against closing a
month), then calls ``ensure_month_open`` for the target month before reading or changing
anything. The workspace row is always locked first, so these locks cannot deadlock.

**Seams.** ``category_usage`` is one of ``services.categories.USAGE_SOURCES`` (``references``
and ``active_months``: planned values change a month's totals); ``workspace_has_budget_lines``
is one of ``services.workspaces.BASE_CURRENCY_LOCKS`` (lines that exist now; removed lines hold
no amount). Writes are audited (``budget_line.*``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import is_database_locked, run_immediate
from assetboard.db.models.ledger import BudgetLine, Category, MonthState
from assetboard.db.repositories import (
    AuditEventRepository,
    BudgetLineRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    MonthStateRepository,
    NotFound,
)
from assetboard.domain.budget import EntryError, planned_minor
from assetboard.domain.categories import Kind, Treatment
from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.i18n.numbers import parse_amount
from assetboard.services.category_usage import CategoryUsage
from assetboard.services.context import (
    Context,
    PermissionDenied,
    Role,
    has_role,
    require_role,
    require_role_now,
)
from assetboard.services.months import MonthClosed, current_month, ensure_month_open

GroupKey = Literal["expense", "saved", "income", "withdrawn", "transfer"]
GROUP_ORDER: tuple[GroupKey, ...] = ("expense", "saved", "income", "withdrawn", "transfer")
SaveOutcome = Literal["created", "updated", "removed", "unchanged"]


class PlanError(Exception):
    code = "invalid"


class InvalidPlannedValue(PlanError):
    """The value or the category cannot take a planned value; ``code`` names why.

    Codes: ``invalid_amount``, ``amount_negative``, ``amount_too_large``,
    ``too_many_decimals``, ``category_archived``, ``category_transfer``. ``currency`` is the
    stored base currency the amount was judged in (for ``too_many_decimals``), when known.
    """

    def __init__(self, code: str, currency: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.currency = currency


@dataclass(frozen=True, slots=True)
class PlanCategory:
    id: UUID
    name: str
    kind: str
    treatment: str
    color: str
    archived: bool


@dataclass(frozen=True, slots=True)
class PlanRow:
    category: PlanCategory
    planned: Money | None  # None: no line


@dataclass(frozen=True, slots=True)
class PlanGroup:
    key: GroupKey
    rows: tuple[PlanRow, ...]


@dataclass(frozen=True, slots=True)
class MonthPlan:
    month: YearMonth
    groups: tuple[PlanGroup, ...]  # in ``GROUP_ORDER``; "transfer" only with lines
    has_lines: bool
    previous: YearMonth | None
    copyable_from_previous: int  # lines a copy would add now

    def group(self, key: GroupKey) -> PlanGroup | None:
        return next((g for g in self.groups if g.key == key), None)


@dataclass(frozen=True, slots=True)
class SavedLine:
    outcome: SaveOutcome
    category_id: UUID
    planned: Money | None  # after the write; None: no line


@dataclass(frozen=True, slots=True)
class CopyResult:
    source: YearMonth
    copied: int
    skipped: int  # lines of categories now archived or transfers
    kept: int  # categories that already had a line in the target month


def group_key(kind: str, treatment: str) -> GroupKey:
    """The summary group of a category (F2.4's blocks; transfers are not counted)."""
    if treatment == Treatment.TRANSFER:
        return "transfer"
    if kind == Kind.EXPENSE:
        return "saved" if treatment == Treatment.SAVINGS else "expense"
    return "withdrawn" if treatment == Treatment.SAVINGS else "income"


def _plannable(category: Category) -> bool:
    """Whether the category can take a **new** line."""
    return category.archived_at is None and category.treatment != Treatment.TRANSFER


def _parse(amount: str, locale: str) -> Decimal:
    """The typed value as a Decimal; an empty field is zero (remove the line).

    Only the syntax and the sign are checked here: the currency-specific checks (decimals, size)
    need the base currency read inside the write (``_minor``), not the possibly stale Context.
    """
    text = amount.strip()
    if not text:
        return Decimal(0)
    try:
        value = parse_amount(text, locale)
    except ValueError:
        raise InvalidPlannedValue("invalid_amount") from None
    if value < 0:
        raise InvalidPlannedValue("amount_negative")
    return value


def _minor(value: Decimal, currency: str) -> int:
    try:
        return planned_minor(value, currency)
    except EntryError as exc:
        raise InvalidPlannedValue(exc.code, currency) from None


def _amount_text(minor: int, currency: str) -> str:
    """The amount as a plain decimal (``12.50``), as stored in audit data."""
    return str(Money(minor, currency).to_decimal())


def _audit_line(
    session: Session,
    ctx: Context,
    action: str,
    line: BudgetLine,
    category: Category,
    extra: Mapping[str, str] | None = None,
) -> None:
    data: dict[str, str | int | bool] = {
        "month": str(YearMonth.from_date(line.month)),
        "category": category.name,
        "amount": _amount_text(line.planned_minor, line.currency),
        "currency": line.currency,
    }
    data |= dict(extra or {})
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action=action,
        entity_type="budget_line",
        entity_id=str(line.id),
        ts=ctx.clock.now(),
        data=data,
    )


def _begin_write(session: Session, ctx: Context, month: YearMonth) -> None:
    """Role, exclusive workspace lock, open-month guard: before any other read."""
    require_role_now(session, ctx, Role.EDITOR)
    CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
    ensure_month_open(session, ctx, month)


def _mark_started(session: Session, ctx: Context, month: YearMonth) -> MonthState:
    """Record the month's first planned-value write (``plan_started_at``), once."""
    repository = MonthStateRepository(session, ctx)
    state = repository.for_month(month) or repository.create(month)
    if state.plan_started_at is None:
        repository.update(state.id, plan_started_at=ctx.clock.now())
    return state


# Reads (every member)


def _plan_category(category: Category) -> PlanCategory:
    return PlanCategory(
        id=category.id,
        name=category.name,
        kind=category.kind,
        treatment=category.treatment,
        color=category.color,
        archived=category.archived_at is not None,
    )


def _copyable(
    source: list[BudgetLine], target: set[UUID], categories: Mapping[UUID, Category]
) -> int:
    return sum(
        1
        for line in source
        if line.category_id not in target and _plannable(categories[line.category_id])
    )


def get_plan(factory: sessionmaker[Session], ctx: Context, month: YearMonth) -> MonthPlan:
    """The month's plan: every active category that is not a transfer, in category order, plus
    archived and transfer categories that have a line in this month."""
    require_role(ctx, Role.VIEWER)
    previous = month.prev()
    with factory() as session:
        repository = BudgetLineRepository(session, ctx)
        lines = {line.category_id: line for line in repository.for_month(month)}
        categories = CategoryRepository(session, ctx).ordered()
        by_id = {c.id: c for c in categories}
        source = repository.for_month(previous) if previous is not None else []
        copyable = _copyable(source, set(lines), by_id)
    rows: dict[GroupKey, list[PlanRow]] = {key: [] for key in GROUP_ORDER}
    # Expense categories first, then income (``ordered`` sorts by kind, then the user's order).
    for category in categories:
        line = lines.get(category.id)
        if line is None and not _plannable(category):
            continue
        planned = Money(line.planned_minor, line.currency) if line is not None else None
        key = group_key(category.kind, category.treatment)
        rows[key].append(PlanRow(_plan_category(category), planned))
    groups = tuple(
        PlanGroup(key, tuple(rows[key])) for key in GROUP_ORDER if key != "transfer" or rows[key]
    )
    return MonthPlan(
        month=month,
        groups=groups,
        has_lines=bool(lines),
        previous=previous,
        copyable_from_previous=copyable,
    )


# Writes (editors and owners)


def set_planned(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    month: YearMonth,
    category_id: UUID,
    amount: str,
    locale: str,
) -> SavedLine:
    """Set the planned value of ``category_id`` in ``month``; zero or empty removes it.

    ``NotFound`` for an unknown category or another workspace's.
    """
    require_role(ctx, Role.EDITOR)
    value = _parse(amount, locale)  # refuse malformed input before the write

    def write(session: Session) -> SavedLine:
        _begin_write(session, ctx, month)
        currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
        minor = _minor(value, currency)
        category = CategoryRepository(session, ctx).get(category_id)
        if category is None:
            raise NotFound
        repository = BudgetLineRepository(session, ctx)
        line = repository.line(month, category.id)
        if line is None:
            if minor == 0:
                return SavedLine("unchanged", category.id, None)
            if category.archived_at is not None:
                raise InvalidPlannedValue("category_archived")
            if category.treatment == Treatment.TRANSFER:
                raise InvalidPlannedValue("category_transfer")
            line = repository.create(
                month=month, category_id=category.id, planned_minor=minor, currency=currency
            )
            _audit_line(session, ctx, "budget_line.created", line, category)
            _mark_started(session, ctx, month)
            return SavedLine("created", category.id, Money(minor, currency))
        if minor == 0:
            _audit_line(session, ctx, "budget_line.deleted", line, category)
            repository.delete(line)
            _mark_started(session, ctx, month)
            return SavedLine("removed", category.id, None)
        if line.planned_minor == minor:
            return SavedLine("unchanged", category.id, Money(minor, line.currency))
        previous = line.planned_minor
        repository.update(line.id, planned_minor=minor)
        extra = {"previous_amount": _amount_text(previous, line.currency)}
        _audit_line(session, ctx, "budget_line.updated", line, category, extra)
        _mark_started(session, ctx, month)
        return SavedLine("updated", category.id, Money(minor, line.currency))

    return run_immediate(factory, write)


def _copy(
    session: Session, ctx: Context, month: YearMonth, source: YearMonth, *, automatic: bool
) -> CopyResult:
    """Copy ``source``'s lines into ``month`` (inside a write that passed ``_begin_write``)."""
    repository = BudgetLineRepository(session, ctx)
    kept_ids = {line.category_id for line in repository.for_month(month)}
    categories = CategoryRepository(session, ctx).ordered()
    order = {c.id: index for index, c in enumerate(categories)}
    by_id = {c.id: c for c in categories}
    copied = skipped = kept = 0
    for line in sorted(repository.for_month(source), key=lambda ln: order[ln.category_id]):
        if line.category_id in kept_ids:
            kept += 1
        elif not _plannable(by_id[line.category_id]):
            skipped += 1
        else:
            repository.create(
                month=month,
                category_id=line.category_id,
                planned_minor=line.planned_minor,
                currency=line.currency,
            )
            copied += 1
    if copied:
        state = _mark_started(session, ctx, month)
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="budget_line.copied",
            entity_type="month",
            entity_id=str(state.id),
            ts=ctx.clock.now(),
            data={
                "month": str(month),
                "source_month": str(source),
                "count": copied,
                "skipped": skipped,
                "automatic": automatic,
            },
        )
    return CopyResult(source=source, copied=copied, skipped=skipped, kept=kept)


def copy_previous_plan(
    factory: sessionmaker[Session], ctx: Context, month: YearMonth
) -> CopyResult | None:
    """Copy the previous month's plan into ``month`` without overwriting any line.

    ``None`` for the first month of the range (it has no previous month).
    """
    require_role(ctx, Role.EDITOR)
    source = month.prev()
    if source is None:
        return None

    def write(session: Session) -> CopyResult:
        _begin_write(session, ctx, month)
        return _copy(session, ctx, month, source, automatic=False)

    return run_immediate(factory, write)


def _auto_copy_due(session: Session, ctx: Context, month: YearMonth, source: YearMonth) -> bool:
    if not CurrentWorkspaceRepository(session, ctx).get().auto_copy_plan:
        return False
    state = MonthStateRepository(session, ctx).for_month(month)
    if state is not None and (state.closed_at is not None or state.plan_started_at is not None):
        return False
    repository = BudgetLineRepository(session, ctx)
    if repository.for_month(month):
        return False
    by_id = {c.id: c for c in CategoryRepository(session, ctx).ordered()}
    return _copyable(repository.for_month(source), set(), by_id) > 0


def auto_copy_on_open(
    factory: sessionmaker[Session], ctx: Context, month: YearMonth
) -> CopyResult | None:
    """The automatic copy when a month page is opened; the result when lines were copied.

    Runs only for editors and owners, for the current month and later ones, while the setting
    is on, the month is open, has no line and never had a planned value. A read decides first,
    so a page view writes only when a copy is due; the write checks everything again after the
    exclusive lock and the month guard. Never fails a page view: a refusal, or SQLite giving up
    on a busy database ("database is locked"), returns ``None`` (the next visit copies).
    """
    if not has_role(ctx, Role.EDITOR) or month < current_month(ctx):
        return None
    source = month.prev()
    if source is None:
        return None
    with factory() as session:
        if not _auto_copy_due(session, ctx, month, source):
            return None

    def write(session: Session) -> CopyResult | None:
        _begin_write(session, ctx, month)
        if not _auto_copy_due(session, ctx, month, source):
            return None
        return _copy(session, ctx, month, source, automatic=True)

    try:
        result = run_immediate(factory, write)
    except (PermissionDenied, MonthClosed):
        return None
    except OperationalError as exc:
        if not is_database_locked(exc):
            raise
        return None  # another write held the database too long: the next visit copies
    return result if result is not None and result.copied else None


# Seams used by other services


def category_usage(session: Session, ctx: Context) -> dict[UUID, CategoryUsage]:
    """The budget lines' part of ``services.categories.USAGE_SOURCES``."""
    return {
        category_id: CategoryUsage(references=usage.references, active_months=usage.months)
        for category_id, usage in BudgetLineRepository(session, ctx).usage_by_category().items()
    }


def workspace_has_budget_lines(session: Session, ctx: Context) -> bool:
    """Part of ``services.workspaces.BASE_CURRENCY_LOCKS``: lines that exist now."""
    return BudgetLineRepository(session, ctx).any_exists()
