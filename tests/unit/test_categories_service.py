"""Categories service (F2.1): rules, role checks, the usage seam and audit events."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from assetboard.clock import FixedClock
from assetboard.db.models.identity import AuditEvent
from assetboard.db.repositories import CategoryRepository, NotFound
from assetboard.domain.categories import SEED, Kind, Treatment
from assetboard.services import categories as service
from assetboard.services.categories import (
    AlreadySeeded,
    CategoryArchived,
    CategoryUsage,
    CategoryView,
    DuplicateName,
    InvalidCategory,
    KindChangeBlocked,
    UsageSource,
    archive_category,
    create_category,
    delete_category,
    get_category,
    list_categories,
    move_category,
    restore_category,
    seed_categories,
    update_category,
    usage_by_category,
)
from assetboard.services.context import Context, PermissionDenied, Role
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


class TickingClock(FixedClock):
    """Each ``now()`` is one second later, so audit events sort in the order they were made."""

    def __init__(self) -> None:
        super().__init__("2026-03-01T12:00:00+00:00")
        self._ticks = 0

    def now(self) -> datetime:
        self._ticks += 1
        return super().now() + timedelta(seconds=self._ticks)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for prepared in workspace_db(tmp_path):
        prepared.clock = TickingClock()
        yield prepared


@pytest.fixture
def team(db: WorkspaceDb) -> WorkspaceDb:
    db.add_member("olga", "owner")
    db.add_member("ed", "editor")
    db.add_member("vic", "viewer")
    return db


def _create(db: WorkspaceDb, name: str, kind: str = "expense", **values: str) -> CategoryView:
    return create_category(
        db.factory,
        db.ctx("ed", "editor"),
        name=name,
        kind=kind,
        treatment=values.get("treatment", "regular"),
        color=values.get("color", "#16a34a"),
    )


def _used(target: UUID, *, references: int = 3, active: int = 2) -> list[UsageSource]:
    """A usage source that reports ``target`` as referenced (what F2.2 transactions will do)."""
    usage = CategoryUsage(references=references, active_transactions=active)
    return [lambda _session, _ctx: {target: usage}]


def _events(db: WorkspaceDb, prefix: str = "category.") -> list[AuditEvent]:
    with db.factory() as session:
        return list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.action.startswith(prefix))
                .order_by(AuditEvent.ts, AuditEvent.id)
            )
        )


def _names(db: WorkspaceDb, kind: str = "expense") -> list[str]:
    listed = list_categories(db.factory, db.ctx("vic", "viewer"))
    return [c.name for c in (listed.expense if kind == "expense" else listed.income)]


# Create


def test_create_cleans_the_name_appends_and_audits(team: WorkspaceDb) -> None:
    first = _create(team, "  Food  ")
    second = _create(team, "Eating   out", color="#F97316")
    assert (first.name, first.sort_order, second.name, second.sort_order) == (
        "Food",
        0,
        "Eating out",
        1,
    )
    assert second.color == "#f97316"
    assert _names(team) == ["Food", "Eating out"]
    event = _events(team)[0]
    assert (event.action, event.entity_type, event.entity_id) == (
        "category.created",
        "category",
        str(first.id),
    )
    assert event.data == {"name": "Food", "kind": "expense", "treatment": "regular"}
    assert event.actor_user_id == team.users["ed"]


@pytest.mark.parametrize(
    ("field", "values"),
    [
        ("name", {"name": "   "}),
        ("name", {"name": "x" * 81}),
        ("name", {"name": "Bad‮name"}),
        ("kind", {"kind": "asset"}),
        ("treatment", {"treatment": "gift"}),
        ("color", {"color": "red"}),
        ("color", {"color": "#12345"}),
    ],
)
def test_create_rejects_invalid_values(
    team: WorkspaceDb, field: str, values: dict[str, str]
) -> None:
    wanted = {"name": "Food", "kind": "expense", "treatment": "regular", "color": "#16a34a"}
    with pytest.raises(InvalidCategory) as caught:
        create_category(team.factory, team.ctx("ed", "editor"), **(wanted | values))
    assert caught.value.field == field
    assert _events(team) == []


def test_names_are_unique_per_kind_ignoring_case_and_unicode_case(team: WorkspaceDb) -> None:
    _create(team, "Żywność")
    with pytest.raises(DuplicateName):
        _create(team, "żYWNOŚĆ")
    with pytest.raises(DuplicateName):
        _create(team, " żywność ")
    _create(team, "Żywność", kind="income")  # another kind: allowed


def test_an_archived_name_can_be_reused(team: WorkspaceDb) -> None:
    old = _create(team, "Food")
    archive_category(team.factory, team.ctx("ed", "editor"), old.id)
    _create(team, "FOOD")
    with pytest.raises(DuplicateName):
        restore_category(team.factory, team.ctx("ed", "editor"), old.id)


def test_a_duplicate_that_passes_the_pre_check_fails_at_the_index(
    team: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create(team, "Food")
    monkeypatch.setattr(service, "_ensure_unique", lambda *_args, **_kwargs: None)
    with pytest.raises(DuplicateName):
        _create(team, "food")


# Update


def test_update_changes_fields_and_lists_them_in_the_audit(team: WorkspaceDb) -> None:
    food = _create(team, "Food")
    ctx = team.ctx("ed", "editor")
    updated = update_category(
        team.factory, ctx, food.id, name="Groceries", kind="expense", treatment="regular",
        color="#0ea5e9",
    )  # fmt: skip
    assert (updated.name, updated.color) == ("Groceries", "#0ea5e9")
    event = _events(team)[-1]
    assert event.action == "category.updated"
    assert event.data == {"name": "Groceries", "fields": "color,name"}


def test_update_without_changes_writes_no_event(team: WorkspaceDb) -> None:
    food = _create(team, "Food")
    update_category(
        team.factory, team.ctx("ed", "editor"), food.id, name="Food", kind="expense",
        treatment="regular", color="#16a34a",
    )  # fmt: skip
    assert [e.action for e in _events(team)] == ["category.created"]


def test_renaming_to_a_case_variant_of_itself_is_allowed(team: WorkspaceDb) -> None:
    food = _create(team, "food")
    updated = update_category(
        team.factory, team.ctx("ed", "editor"), food.id, name="Food", kind="expense",
        treatment="regular", color="#16a34a",
    )  # fmt: skip
    assert updated.name == "Food"


def test_renaming_onto_another_active_name_is_refused(team: WorkspaceDb) -> None:
    _create(team, "Food")
    other = _create(team, "Other")
    with pytest.raises(DuplicateName):
        update_category(
            team.factory, team.ctx("ed", "editor"), other.id, name="FOOD", kind="expense",
            treatment="regular", color="#16a34a",
        )  # fmt: skip


def test_kind_change_is_blocked_when_the_category_is_in_use(team: WorkspaceDb) -> None:
    food = _create(team, "Food")
    with pytest.raises(KindChangeBlocked):
        update_category(
            team.factory, team.ctx("ed", "editor"), food.id, name="Food", kind="income",
            treatment="regular", color="#16a34a", usage_sources=_used(food.id),
        )  # fmt: skip
    assert get_category(team.factory, team.ctx("vic", "viewer"), food.id).kind == "expense"


def test_kind_change_is_blocked_by_soft_deleted_references_alone(team: WorkspaceDb) -> None:
    food = _create(team, "Food")
    with pytest.raises(KindChangeBlocked):
        update_category(
            team.factory, team.ctx("ed", "editor"), food.id, name="Food", kind="income",
            treatment="regular", color="#16a34a",
            usage_sources=_used(food.id, references=1, active=0),
        )  # fmt: skip


def test_kind_change_of_an_unused_category_moves_it_to_the_end_of_the_new_kind(
    team: WorkspaceDb,
) -> None:
    _create(team, "Salary", kind="income")
    misfiled = _create(team, "Bonus")
    update_category(
        team.factory, team.ctx("ed", "editor"), misfiled.id, name="Bonus", kind="income",
        treatment="regular", color="#16a34a",
    )  # fmt: skip
    assert _names(team, "income") == ["Salary", "Bonus"]
    assert _names(team) == []
    assert _events(team)[-1].data["fields"] == "kind"


def test_treatment_change_is_allowed_when_in_use_and_records_affected_count(
    team: WorkspaceDb,
) -> None:
    savings = _create(team, "Savings")
    updated = update_category(
        team.factory, team.ctx("ed", "editor"), savings.id, name="Savings", kind="expense",
        treatment="savings", color="#16a34a", usage_sources=_used(savings.id, active=7),
    )  # fmt: skip
    assert updated.treatment == "savings"
    assert updated.active_transactions == 7
    assert _events(team)[-1].data == {
        "name": "Savings",
        "fields": "treatment",
        "previous_treatment": "regular",
        "treatment": "savings",
        "affected_transactions": 7,
    }


def test_archived_categories_cannot_be_edited_or_moved(team: WorkspaceDb) -> None:
    food = _create(team, "Food")
    ctx = team.ctx("ed", "editor")
    archive_category(team.factory, ctx, food.id)
    with pytest.raises(CategoryArchived):
        update_category(
            team.factory, ctx, food.id, name="Food", kind="expense", treatment="regular",
            color="#16a34a",
        )  # fmt: skip
    with pytest.raises(CategoryArchived):
        move_category(team.factory, ctx, food.id, "up")


# Order


def test_move_swaps_with_the_neighbour_and_is_a_no_op_at_the_edges(team: WorkspaceDb) -> None:
    a, b, c = (_create(team, name) for name in ("A", "B", "C"))
    ctx = team.ctx("ed", "editor")
    move_category(team.factory, ctx, c.id, "up")
    assert _names(team) == ["A", "C", "B"]
    move_category(team.factory, ctx, a.id, "up")
    move_category(team.factory, ctx, b.id, "down")
    assert _names(team) == ["A", "C", "B"]
    move_category(team.factory, ctx, a.id, "down")
    assert _names(team) == ["C", "A", "B"]
    orders = [c.sort_order for c in list_categories(team.factory, ctx).expense]
    assert orders == [0, 1, 2]
    assert [e.action for e in _events(team)] == ["category.created"] * 3  # moves: not audited


def test_move_skips_archived_categories(team: WorkspaceDb) -> None:
    a, b, c = (_create(team, name) for name in ("A", "B", "C"))
    ctx = team.ctx("ed", "editor")
    archive_category(team.factory, ctx, b.id)
    move_category(team.factory, ctx, c.id, "up")
    assert _names(team) == ["C", "A"]
    assert a.id != c.id


# Archive, restore, delete


def test_archive_hides_from_active_lists_and_restore_appends(team: WorkspaceDb) -> None:
    a, _b = _create(team, "A"), _create(team, "B")
    ctx = team.ctx("ed", "editor")
    archive_category(team.factory, ctx, a.id)
    archive_category(team.factory, ctx, a.id)  # idempotent: one event
    listed = list_categories(team.factory, ctx)
    assert [c.name for c in listed.expense] == ["B"]
    assert [c.name for c in listed.archived] == ["A"]
    restore_category(team.factory, ctx, a.id)
    assert _names(team) == ["B", "A"]
    assert [e.action for e in _events(team)][2:] == ["category.archived", "category.restored"]
    assert _events(team)[2].data == {"name": "A", "in_use": False}


def test_delete_removes_an_unused_category(team: WorkspaceDb) -> None:
    food = _create(team, "Food")
    ctx = team.ctx("ed", "editor")
    assert delete_category(team.factory, ctx, food.id) == "deleted"
    with pytest.raises(NotFound):
        get_category(team.factory, ctx, food.id)
    assert _events(team)[-1].data == {"name": "Food", "kind": "expense"}


def test_delete_archives_a_category_in_use_instead(team: WorkspaceDb) -> None:
    food = _create(team, "Food")
    ctx = team.ctx("ed", "editor")
    assert delete_category(team.factory, ctx, food.id, usage_sources=_used(food.id)) == "archived"
    assert get_category(team.factory, ctx, food.id).archived
    assert _events(team)[-1].action == "category.archived"
    assert _events(team)[-1].data == {"name": "Food", "in_use": True}
    # Deleting it again keeps it archived and writes nothing new.
    assert delete_category(team.factory, ctx, food.id, usage_sources=_used(food.id)) == "archived"
    assert len(_events(team)) == 2


def test_usage_sources_are_summed_and_shown(team: WorkspaceDb) -> None:
    food = _create(team, "Food")

    def source(
        refs: int, active: int
    ) -> Callable[[Session, Context], Mapping[UUID, CategoryUsage]]:
        return lambda _session, _ctx: {food.id: CategoryUsage(refs, active)}

    sources = [source(2, 1), source(3, 0)]
    with team.factory() as session:
        totals = usage_by_category(session, team.ctx("vic", "viewer"), sources)
    assert totals == {food.id: CategoryUsage(references=5, active_transactions=1)}
    listed = list_categories(team.factory, team.ctx("vic", "viewer"), usage_sources=sources)
    assert listed.expense[0].in_use and listed.expense[0].references == 5
    assert service.USAGE_SOURCES == []  # F2.1 registers none


# Seed


def test_seed_creates_the_suggested_set(team: WorkspaceDb) -> None:
    assert list_categories(team.factory, team.ctx("vic", "viewer")).can_seed
    assert seed_categories(team.factory, team.ctx("ed", "editor"), locale="en") == len(SEED) == 23
    listed = list_categories(team.factory, team.ctx("vic", "viewer"))
    assert not listed.can_seed
    expense = [(c.name, c.treatment) for c in listed.expense]
    income = [(c.name, c.treatment) for c in listed.income]
    assert expense[:3] == [("Food", "regular"), ("Eating out", "regular"), ("Gifts", "regular")]
    assert expense[-3:] == [
        ("Other", "regular"),
        ("Savings", "savings"),
        ("Investments", "savings"),
    ]
    assert len(expense) == 17
    assert income == [
        ("Salary", "regular"),
        ("Bonus", "regular"),
        ("Interest", "regular"),
        ("Sale of items", "regular"),
        ("Other", "regular"),
        ("Savings withdrawal", "savings"),
    ]
    assert [c.sort_order for c in listed.income] == list(range(6))
    events = _events(team)
    assert [(e.action, e.data) for e in events] == [("category.seeded", {"count": 23})]


def test_seed_names_are_translated(team: WorkspaceDb) -> None:
    seed_categories(team.factory, team.ctx("ed", "editor"), locale="pl")
    listed = list_categories(team.factory, team.ctx("vic", "viewer"))
    assert listed.expense[0].name == "Jedzenie"
    assert listed.income[0].name == "Wynagrodzenie"
    assert listed.income[-1].name == "Wypłata z oszczędności"
    assert {c.kind for c in listed.expense} == {Kind.EXPENSE}
    assert {c.treatment for c in listed.income[-1:]} == {Treatment.SAVINGS}


def test_seed_is_refused_once_any_category_exists_even_archived(team: WorkspaceDb) -> None:
    food = _create(team, "Food")
    archive_category(team.factory, team.ctx("ed", "editor"), food.id)
    assert not list_categories(team.factory, team.ctx("vic", "viewer")).can_seed
    with pytest.raises(AlreadySeeded):
        seed_categories(team.factory, team.ctx("ed", "editor"), locale="en")


# Roles and workspaces


WRITES: dict[str, Callable[[WorkspaceDb, Context, UUID], object]] = {
    "create": lambda db, ctx, _id: create_category(
        db.factory, ctx, name="New", kind="expense", treatment="regular", color="#16a34a"
    ),
    "update": lambda db, ctx, cid: update_category(
        db.factory,
        ctx,
        cid,
        name="Renamed",
        kind="expense",
        treatment="regular",
        color="#16a34a",
    ),  # fmt: skip
    "move": lambda db, ctx, cid: move_category(db.factory, ctx, cid, "down"),
    "archive": lambda db, ctx, cid: archive_category(db.factory, ctx, cid),
    "restore": lambda db, ctx, cid: restore_category(db.factory, ctx, cid),
    "delete": lambda db, ctx, cid: delete_category(db.factory, ctx, cid),
    "seed": lambda db, ctx, _id: seed_categories(db.factory, ctx, locale="en"),
}


@pytest.mark.parametrize("write", sorted(WRITES))
def test_viewers_cannot_write(team: WorkspaceDb, write: str) -> None:
    food = _create(team, "Food")
    before = list_categories(team.factory, team.ctx("vic", "viewer"))
    with pytest.raises(PermissionDenied):
        WRITES[write](team, team.ctx("vic", "viewer"), food.id)
    assert list_categories(team.factory, team.ctx("vic", "viewer")) == before
    assert len(_events(team)) == 1


@pytest.mark.parametrize("write", sorted(WRITES))
def test_a_stale_editor_context_is_refused_inside_the_transaction(
    team: WorkspaceDb, write: str
) -> None:
    food = _create(team, "Food")
    team.add_member("dee", "viewer")  # demoted: the Context still says editor
    with pytest.raises(PermissionDenied):
        WRITES[write](team, team.ctx("dee", "editor"), food.id)
    assert len(_events(team)) == 1


@pytest.mark.parametrize("role", ["editor", "owner"])
@pytest.mark.parametrize("write", sorted(WRITES))
def test_editors_and_owners_can_write(db: WorkspaceDb, write: str, role: str) -> None:
    db.add_member("someone", role)
    ctx = db.ctx("someone", role)
    target = uuid4()
    if write != "seed":
        target = create_category(
            db.factory, ctx, name="Target", kind="expense", treatment="regular", color="#16a34a"
        ).id
    if write == "restore":
        archive_category(db.factory, ctx, target)
    WRITES[write](db, ctx, target)
    assert _events(db)[-1].actor_user_id == db.users["someone"] or write == "move"


def test_viewers_can_read(team: WorkspaceDb) -> None:
    food = _create(team, "Food")
    ctx = team.ctx("vic", "viewer")
    assert [c.id for c in list_categories(team.factory, ctx).expense] == [food.id]
    assert get_category(team.factory, ctx, food.id).name == "Food"


def test_another_workspaces_category_is_not_found(team: WorkspaceDb) -> None:
    food = _create(team, "Food")
    other = team.add_workspace("Other")
    team.add_member("ed", "editor", other)
    foreign = team.ctx("ed", "editor", other)
    assert list_categories(team.factory, foreign).expense == ()
    for write in ("update", "move", "archive", "restore", "delete"):
        with pytest.raises(NotFound):
            WRITES[write](team, foreign, food.id)
    with pytest.raises(NotFound):
        get_category(team.factory, foreign, food.id)
    assert get_category(team.factory, team.ctx("ed", "editor"), food.id).name == "Food"


@pytest.mark.parametrize("write", sorted(WRITES))
def test_the_role_is_checked_before_any_database_work(
    team: WorkspaceDb, monkeypatch: pytest.MonkeyPatch, write: str
) -> None:
    monkeypatch.setattr(service, "run_immediate", lambda *_args: pytest.fail("no write"))
    with pytest.raises(PermissionDenied) as caught:
        WRITES[write](team, team.ctx("vic", "viewer"), uuid4())
    assert caught.value.required is Role.EDITOR


# Review follow-ups


def test_archive_records_whether_the_category_is_in_use(team: WorkspaceDb) -> None:
    used, unused = _create(team, "Used"), _create(team, "Unused")
    ctx = team.ctx("ed", "editor")
    archive_category(team.factory, ctx, used.id, usage_sources=_used(used.id))
    archive_category(team.factory, ctx, unused.id, usage_sources=_used(used.id))
    archived = {e.data["name"]: e.data["in_use"] for e in _events(team, "category.archived")}
    assert archived == {"Used": True, "Unused": False}


@pytest.mark.parametrize("direction", ["", "left", "UP", "sideways"])
def test_move_refuses_unknown_directions(team: WorkspaceDb, direction: str) -> None:
    food = _create(team, "Food")
    with pytest.raises(InvalidCategory) as caught:
        move_category(team.factory, team.ctx("ed", "editor"), food.id, direction)
    assert caught.value.code == "invalid_direction"


def test_a_concurrent_seed_that_loses_the_race_reads_as_already_seeded(
    team: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another seed committed "Food" after this one counted zero categories.
    _create(team, "Food")
    monkeypatch.setattr(CategoryRepository, "count", lambda _self: 0)
    with pytest.raises(AlreadySeeded):
        seed_categories(team.factory, team.ctx("ed", "editor"), locale="en")
    monkeypatch.undo()
    assert _names(team) == ["Food"]
    assert _events(team, "category.seeded") == []


def test_emoji_names_with_joiners_are_accepted(team: WorkspaceDb) -> None:
    family = _create(team, "👨‍👩‍👧")
    assert family.name == "👨‍👩‍👧"
