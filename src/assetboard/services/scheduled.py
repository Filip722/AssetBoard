from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from uuid import UUID

import holidays as holiday_calendars
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.engine import run_immediate
from assetboard.db.models.scheduled import ScheduledItem
from assetboard.db.repositories.audit import AuditEventRepository
from assetboard.db.repositories.base import WorkspaceScope
from assetboard.db.repositories.category import CategoryRepository
from assetboard.db.repositories.scheduled import (
    ScheduledItemRepository,
    ScheduledOccurrenceRepository,
)
from assetboard.db.repositories.workspace import CurrentWorkspaceRepository
from assetboard.domain.money import Money
from assetboard.domain.months import MAX_YEAR
from assetboard.domain.recurrence import (
    Frequency,
    MonthDay,
    RecurrenceRule,
    WeekendPolicy,
    occurrences,
)
from assetboard.domain.scheduled import AmountMode, AmountState, ItemAction, OccurrenceStatus
from assetboard.domain.transactions import MAX_AMOUNT_MINOR, TransactionDirection, clean_description
from assetboard.services.category_usage import CategoryUsage
from assetboard.services.context import Context, Role, require_role, require_role_now


@dataclass(frozen=True, slots=True)
class ScheduledInput:
    name: str
    direction: TransactionDirection
    category_id: UUID
    amount: Money
    rule: RecurrenceRule
    amount_mode: AmountMode = AmountMode.FIXED
    notes: str = ""


@dataclass(frozen=True, slots=True)
class ScheduledView:
    id: UUID
    values: ScheduledInput
    paused: bool
    archived: bool


def _rule(item: ScheduledItem) -> RecurrenceRule:
    choice = item.month_day
    return RecurrenceRule(
        start_date=item.start_date,
        end_date=item.end_date,
        max_count=item.max_count,
        freq=Frequency(item.freq),
        interval=item.interval,
        weekdays=tuple(item.weekdays),
        month_day=int(choice) if choice.lstrip("-").isdigit() else MonthDay(choice),
        month=item.month,
        weekend_policy=WeekendPolicy(item.weekend_policy),
    )


def _values(item: ScheduledItem) -> ScheduledInput:
    return ScheduledInput(
        item.name,
        TransactionDirection(item.direction),
        item.category_id,
        Money(item.amount_minor, item.currency),
        _rule(item),
        AmountMode(item.amount_mode),
        item.notes,
    )


def _holidays(
    country: str | None, rule: RecurrenceRule
) -> holiday_calendars.HolidayBase | frozenset[date]:
    if country is None or (
        rule.weekend_policy == WeekendPolicy.NONE
        and (rule.freq != Frequency.MONTHLY or isinstance(rule.month_day, int))
    ):
        return frozenset()
    try:
        return holiday_calendars.country_holidays(country, language="en")
    except NotImplementedError:
        raise ValueError("unsupported_holidays") from None


def _horizon(today: date, months: int) -> date:
    year, month = divmod(today.year * 12 + today.month - 1 + months, 12)
    if year > MAX_YEAR:
        return date(MAX_YEAR, 12, 31)
    return date(year, month + 1, min(today.day, monthrange(year, month + 1)[1]))


def preview(factory: sessionmaker[Session], ctx: Context, rule: RecurrenceRule) -> list[date]:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        return occurrences(
            rule,
            ctx.clock.today(workspace.timezone),
            date(MAX_YEAR, 12, 31),
            _holidays(workspace.country_code, rule),
            limit=6,
        )


def list_items(factory: sessionmaker[Session], ctx: Context) -> list[ScheduledView]:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        return [
            ScheduledView(
                row.id, _values(row), row.paused_at is not None, row.archived_at is not None
            )
            for row in ScheduledItemRepository(session, ctx).ordered()
        ]


def get_item(factory: sessionmaker[Session], ctx: Context, item_id: UUID) -> ScheduledInput:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        return _values(ScheduledItemRepository(session, ctx).get_or_raise(item_id))


def _generate(
    session: Session,
    scope: WorkspaceScope,
    clock: Clock,
    item: ScheduledItem,
    settings: Settings,
    *,
    include_past: bool = False,
) -> int:
    if item.paused_at is not None or item.archived_at is not None:
        return 0
    workspace = CurrentWorkspaceRepository(session, scope).get()
    today = clock.today(workspace.timezone)
    rule = _rule(item)
    due_dates = occurrences(
        rule,
        date(1900, 1, 1) if include_past else today,
        _horizon(today, settings.recurrence_horizon_months),
        _holidays(workspace.country_code, rule),
    )
    repository = ScheduledOccurrenceRepository(session, scope)
    existing = {row.due_date for row in repository.for_item(item.id)}
    for day in due_dates:
        if day not in existing:
            repository.create(
                scheduled_item_id=item.id,
                direction=item.direction,
                category_id=item.category_id,
                due_date=day,
                expected_minor=item.amount_minor,
                currency=item.currency,
                amount_state=AmountState.CONFIRMED
                if item.amount_mode == AmountMode.FIXED
                else AmountState.ESTIMATED,
                status=OccurrenceStatus.PENDING,
                label=item.name,
            )
    return len(set(due_dates) - existing)


def generate_workspace(
    factory: sessionmaker[Session],
    scope: WorkspaceScope,
    clock: Clock,
    settings: Settings,
) -> int:
    """Trusted scheduler entry point; it has an explicit tenant scope, not an impersonated user."""

    def write(session: Session) -> int:
        CurrentWorkspaceRepository(session, scope).lock(exclusive=True)
        return sum(
            _generate(session, scope, clock, item, settings)
            for item in ScheduledItemRepository(session, scope).ordered(active_only=True)
        )

    return run_immediate(factory, write)


def save_item(
    factory: sessionmaker[Session],
    ctx: Context,
    values: ScheduledInput,
    settings: Settings,
    *,
    item_id: UUID | None = None,
    include_past: bool = False,
) -> UUID:
    require_role(ctx, Role.EDITOR)
    name = clean_description(values.name)
    notes = clean_description(values.notes)
    if not name or not 0 < values.amount.amount_minor <= MAX_AMOUNT_MINOR:
        raise ValueError("invalid_item")

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        if values.amount.currency != workspace.base_currency:
            raise ValueError("invalid_currency")
        category = CategoryRepository(session, ctx).get_or_raise(values.category_id)
        if category.kind != values.direction or category.archived_at is not None:
            raise ValueError("invalid_category")
        repository = ScheduledItemRepository(session, ctx)
        rule = values.rule
        fields = {
            "name": name,
            "notes": notes,
            "direction": values.direction,
            "category_id": values.category_id,
            "amount_minor": values.amount.amount_minor,
            "currency": values.amount.currency,
            "amount_mode": values.amount_mode,
            "start_date": rule.start_date,
            "end_date": rule.end_date,
            "max_count": rule.max_count,
            "freq": rule.freq,
            "interval": rule.interval,
            "weekdays": list(rule.weekdays),
            "month_day": str(rule.month_day),
            "month": rule.month,
            "weekend_policy": rule.weekend_policy,
        }
        if item_id is None:
            item = repository.create(**fields)
        else:
            item = repository.get_or_raise(item_id)
            if item.archived_at is not None:
                raise ValueError("archived_item")
            item = repository.update(item_id, **fields)
            ScheduledOccurrenceRepository(session, ctx).delete_pending_future(
                item.id, ctx.clock.today(workspace.timezone)
            )
        _generate(
            session,
            ctx,
            ctx.clock,
            item,
            settings,
            include_past=include_past if item_id is None else False,
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="scheduled.updated" if item_id else "scheduled.created",
            entity_type="scheduled_item",
            entity_id=str(item.id),
            ts=ctx.clock.now(),
        )
        return item.id

    return run_immediate(factory, write)


def change_state(
    factory: sessionmaker[Session],
    ctx: Context,
    item_id: UUID,
    action: ItemAction,
    settings: Settings,
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        repository = ScheduledItemRepository(session, ctx)
        item = repository.get_or_raise(item_id)
        if item.archived_at is not None:
            raise ValueError("archived_item")
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        rows = ScheduledOccurrenceRepository(session, ctx)
        if action == ItemAction.RESUME:
            repository.update(item.id, paused_at=None)
            _generate(session, ctx, ctx.clock, item, settings)
        else:
            rows.delete_pending_future(item.id, ctx.clock.today(workspace.timezone))
            if action == ItemAction.DELETE and not rows.for_item(item.id):
                repository.delete(item.id)
            else:
                repository.update(
                    item.id,
                    **{
                        "paused_at"
                        if action == ItemAction.PAUSE
                        else "archived_at": ctx.clock.now()
                    },
                )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action=f"scheduled.{action}",
            entity_type="scheduled_item",
            entity_id=str(item_id),
            ts=ctx.clock.now(),
        )

    run_immediate(factory, write)


def category_usage(session: Session, ctx: Context) -> dict[UUID, CategoryUsage]:
    counts: dict[UUID, int] = {}
    for repository in (
        ScheduledItemRepository(session, ctx),
        ScheduledOccurrenceRepository(session, ctx),
    ):
        for key, count in repository.category_counts().items():
            counts[key] = counts.get(key, 0) + count
    return {key: CategoryUsage(references=count) for key, count in counts.items()}


def workspace_has_scheduled_amounts(session: Session, ctx: Context) -> bool:
    return bool(
        ScheduledItemRepository(session, ctx).list()
        or ScheduledOccurrenceRepository(session, ctx).list()
    )
