from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.identity import AuditEvent, Membership, Workspace
from assetboard.db.repositories import CategoryRepository, NotFound
from assetboard.db.repositories.scheduled import (
    ScheduledOccurrenceRepository,
)
from assetboard.domain.money import Money
from assetboard.domain.recurrence import Frequency, MonthDay, RecurrenceRule, WeekendPolicy
from assetboard.domain.scheduled import ItemAction
from assetboard.domain.transactions import TransactionDirection
from assetboard.services import scheduled
from assetboard.services.categories import KindChangeBlocked, delete_category, update_category
from assetboard.services.context import PermissionDenied
from assetboard.services.workspaces import BaseCurrencyLocked, update_settings
from tests.tenant.factories import make_category
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def db(tmp_path):
    yield from workspace_db(tmp_path)


@pytest.fixture
def team(db: WorkspaceDb):
    db.add_member("owner", "owner")
    db.add_member("viewer", "viewer")
    return db


def data(db: WorkspaceDb, **changes):
    with db.factory.begin() as session:
        category = CategoryRepository(session, db.ctx("owner", "owner")).add(
            make_category(session, db.workspace_id)
        )
    return replace(
        scheduled.ScheduledInput(
            "Rent",
            TransactionDirection.EXPENSE,
            category.id,
            Money(10000, "EUR"),
            RecurrenceRule(date(2026, 1, 1), month_day=1),
        ),
        **changes,
    )


def rows(db, item_id):
    with db.factory() as session:
        return ScheduledOccurrenceRepository(session, db.ctx("owner", "owner")).for_item(item_id)


def test_generator_horizon_past_opt_in_and_idempotence(team):
    settings = Settings(_env_file=None)
    ctx = team.ctx("owner", "owner")
    item = scheduled.save_item(team.factory, ctx, data(team), settings)
    assert [row.due_date for row in rows(team, item)] == [
        date(2026 + month // 12, month % 12 + 1, 1) for month in range(2, 16)
    ]
    assert scheduled.generate_workspace(team.factory, ctx, team.clock, settings) == 0
    past = scheduled.save_item(team.factory, ctx, data(team), settings, include_past=True)
    assert rows(team, past)[0].due_date == date(2026, 1, 1)
    limited = scheduled.save_item(
        team.factory, ctx, data(team), settings.model_copy(update={"recurrence_horizon_months": 1})
    )
    assert [row.due_date for row in rows(team, limited)] == [date(2026, 3, 1), date(2026, 4, 1)]
    assert scheduled.get_item(team.factory, ctx, item).name == "Rent"
    assert len(scheduled.list_items(team.factory, team.ctx("viewer", "viewer"))) == 3


def test_edit_preserves_history_posted_and_skipped(team):
    ctx = team.ctx("owner", "owner")
    settings = Settings(_env_file=None)
    values = data(team)
    item = scheduled.save_item(team.factory, ctx, values, settings, include_past=True)
    before = rows(team, item)
    with team.factory.begin() as session:
        repository = ScheduledOccurrenceRepository(session, ctx)
        repository.update(before[3].id, status="posted")
        repository.update(before[4].id, status="skipped")
    scheduled.save_item(
        team.factory,
        ctx,
        replace(
            values,
            name="New rent",
            amount=Money(12000, "EUR"),
            rule=replace(values.rule, month_day=15),
        ),
        settings,
        item_id=item,
        include_past=True,
    )
    after = rows(team, item)
    preserved = {row.id: row for row in after}
    assert all(row.id in preserved for row in (before[0], before[1], before[3], before[4]))
    assert before[2].id not in preserved
    assert all(
        preserved[row.id].expected_minor == 10000
        for row in (before[0], before[1], before[3], before[4])
    )
    assert any(row.expected_minor == 12000 and row.due_date == date(2026, 3, 15) for row in after)


def test_pause_resume_archive_and_delete_history(team):
    ctx, settings = team.ctx("owner", "owner"), Settings(_env_file=None)
    item = scheduled.save_item(team.factory, ctx, data(team), settings, include_past=True)
    scheduled.change_state(team.factory, ctx, item, ItemAction.PAUSE, settings)
    assert [row.due_date for row in rows(team, item)] == [date(2026, 1, 1), date(2026, 2, 1)]
    assert scheduled.generate_workspace(team.factory, ctx, team.clock, settings) == 0
    scheduled.change_state(team.factory, ctx, item, ItemAction.RESUME, settings)
    assert len(rows(team, item)) > 2
    scheduled.change_state(team.factory, ctx, item, ItemAction.DELETE, settings)
    assert scheduled.list_items(team.factory, ctx)[0].archived
    assert len(rows(team, item)) == 2
    with team.factory() as session:
        actions = set(
            session.scalars(select(AuditEvent.action).where(AuditEvent.entity_id == str(item)))
        )
        assert "scheduled.archive" in actions
        assert "scheduled.delete" not in actions
    with pytest.raises(ValueError):
        scheduled.change_state(team.factory, ctx, item, ItemAction.RESUME, settings)
    empty = scheduled.save_item(team.factory, ctx, data(team), settings)
    scheduled.change_state(team.factory, ctx, empty, ItemAction.ARCHIVE, settings)
    assert rows(team, empty) == []
    fresh = scheduled.save_item(team.factory, ctx, data(team), settings)
    scheduled.change_state(team.factory, ctx, fresh, ItemAction.DELETE, settings)
    with pytest.raises(NotFound):
        scheduled.get_item(team.factory, ctx, fresh)
    with team.factory() as session:
        actions = set(
            session.scalars(select(AuditEvent.action).where(AuditEvent.entity_id == str(fresh)))
        )
        assert "scheduled.delete" in actions


def test_roles_validation_and_tenant_isolation(team):
    ctx, settings = team.ctx("owner", "owner"), Settings(_env_file=None)
    values = data(team)
    with pytest.raises(PermissionDenied):
        scheduled.save_item(team.factory, team.ctx("viewer", "viewer"), values, settings)
    with team.factory.begin() as session:
        member = session.get(Membership, team.memberships["viewer"])
        member.role = "editor"
    stale = team.ctx("viewer", "editor")
    with team.factory.begin() as session:
        session.get(Membership, team.memberships["viewer"]).role = "viewer"
    with pytest.raises(PermissionDenied):
        scheduled.save_item(team.factory, stale, values, settings)
    for changed in (
        replace(values, name=""),
        replace(values, amount=Money(1, "PLN")),
        replace(values, direction=TransactionDirection.INCOME),
    ):
        with pytest.raises(ValueError):
            scheduled.save_item(team.factory, ctx, changed, settings)
    other = team.add_workspace("Other")
    team.add_member("owner", "owner", other)
    other_ctx = team.ctx("owner", "owner", other)
    item = scheduled.save_item(team.factory, ctx, values, settings)
    assert scheduled.list_items(team.factory, other_ctx) == []
    for action in (
        lambda: scheduled.get_item(team.factory, other_ctx, item),
        lambda: scheduled.save_item(team.factory, other_ctx, values, settings),
        lambda: scheduled.change_state(team.factory, other_ctx, item, ItemAction.PAUSE, settings),
    ):
        with pytest.raises(NotFound):
            action()


def test_country_holidays_timezone_and_category_currency_guards(team):
    ctx, settings = team.ctx("owner", "owner"), Settings(_env_file=None)
    with team.factory.begin() as session:
        workspace = session.get(Workspace, team.workspace_id)
        workspace.country_code, workspace.timezone = "PL", "Europe/Warsaw"
    clock = FixedClock("2026-04-30T23:30:00+00:00")
    ctx = replace(ctx, clock=clock)
    values = data(team, rule=RecurrenceRule(date(2026, 5, 1), weekend_policy=WeekendPolicy.NEXT))
    assert scheduled.preview(team.factory, ctx, values.rule)[0] == date(2026, 5, 4)
    item = scheduled.save_item(team.factory, ctx, values, settings)
    assert rows(team, item)[0].due_date == date(2026, 5, 4)
    with pytest.raises(KindChangeBlocked):
        update_category(
            team.factory,
            ctx,
            values.category_id,
            name="Other",
            kind="income",
            treatment="regular",
            color="#16a34a",
        )
    assert delete_category(team.factory, ctx, values.category_id) == "archived"
    with pytest.raises(ValueError):
        scheduled.save_item(team.factory, ctx, values, settings)
    with pytest.raises(BaseCurrencyLocked):
        update_settings(
            team.factory,
            ctx,
            name="Home",
            base_currency="PLN",
            country_code="PL",
            timezone="Europe/Warsaw",
            locale="en",
            auto_copy_plan=False,
        )
    with team.factory() as session:
        assert scheduled.category_usage(session, ctx)[values.category_id].references > 1
        assert scheduled.workspace_has_scheduled_amounts(session, ctx)
        assert {event.action for event in session.scalars(select(AuditEvent))} >= {
            "scheduled.created"
        }


def test_missing_and_unsupported_country_holiday_rules(team):
    ctx = team.ctx("owner", "owner")
    rule = RecurrenceRule(date(2026, 5, 1), weekend_policy=WeekendPolicy.NEXT)
    assert scheduled.preview(team.factory, ctx, rule)[0] == date(2026, 5, 1)
    with team.factory.begin() as session:
        session.get(Workspace, team.workspace_id).country_code = "ZZ"
    with pytest.raises(ValueError, match="unsupported_holidays"):
        scheduled.preview(team.factory, ctx, rule)
    with pytest.raises(ValueError, match="unsupported_holidays"):
        scheduled.preview(
            team.factory,
            ctx,
            replace(rule, month_day=MonthDay.FIRST_WORKING, weekend_policy=WeekendPolicy.NONE),
        )
    assert scheduled.preview(team.factory, ctx, replace(rule, weekend_policy=WeekendPolicy.NONE))[
        0
    ] == date(2026, 5, 1)
    weekly = replace(
        rule,
        freq=Frequency.WEEKLY,
        weekdays=(0,),
        month_day=MonthDay.FIRST_WORKING,
        weekend_policy=WeekendPolicy.NONE,
    )
    assert scheduled.preview(team.factory, ctx, weekly)[0] == date(2026, 5, 4)


def test_simultaneous_generation_never_duplicates(team):
    ctx, settings = team.ctx("owner", "owner"), Settings(_env_file=None)
    item = scheduled.save_item(team.factory, ctx, data(team), settings)
    later = FixedClock("2026-04-01T12:00:00+00:00")
    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(
            pool.map(
                lambda _: scheduled.generate_workspace(team.factory, ctx, later, settings), range(2)
            )
        )
    assert sorted(counts) == [0, 1]
    dates = [row.due_date for row in rows(team, item)]
    assert len(dates) == len(set(dates))
