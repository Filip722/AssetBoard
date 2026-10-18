"""Tenant isolation (F1.2): every workspace-owned model, through its repository, both directions.

Adding a workspace-owned model without a repository and a factory in ``tests/tenant/factories.py``
fails this module by design. See docs/architecture.md, "How to add a new workspace-owned model".
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import ForeignKey, ForeignKeyConstraint, MetaData, String, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from assetboard.clock import FixedClock
from assetboard.db.base import (
    CONSTRAINT_NAMING_CONVENTION,
    AppendOnlyViolationError,
    Base,
    IdMixin,
    TimestampMixin,
    WorkspaceOwned,
    WorkspaceReassignmentError,
    WorkspaceScopedMixin,
)
from assetboard.db.engine import create_database_engine
from assetboard.db.models.identity import AuditEvent, Invitation, Membership, User, Workspace
from assetboard.db.repositories import (
    UPDATABLE_FIELDS,
    AuditEventRepository,
    AuditFilter,
    CategoryRepository,
    CurrentWorkspaceRepository,
    ForeignWorkspace,
    InstanceAuditReader,
    InvitationRepository,
    InvitationTokenReader,
    MembershipRepository,
    MonthStateRepository,
    NotFound,
    ScopedRepository,
    TransactionRepository,
    UserMembershipReader,
)
from assetboard.db.repositories.base import models_marked_workspace_owned, repositories_for
from assetboard.domain.months import YearMonth
from tests.tenant.coverage import (
    BASE_METHOD_COVERAGE,
    CURRENT_WORKSPACE_METHOD_COVERAGE,
    EXTRA_METHOD_COVERAGE,
    INSTANCE_AUDIT_READER_METHOD_COVERAGE,
    READER_METHOD_COVERAGE,
    TOKEN_READER_METHOD_COVERAGE,
)
from tests.tenant.factories import TENANT_CASES
from tests.tenant.harness import (
    ARCHITECTURE_HINT,
    check_isolation,
    context_for,
    discovery_problems,
    non_composite_workspace_fks,
    owned_models,
    public_methods,
    raw_row,
    relationships_into_owned_models,
    uncovered_methods,
)


def _workspace(session: Session, name: str) -> Workspace:
    workspace = Workspace(name=name, base_currency="EUR", locale="en", timezone="UTC")
    session.add(workspace)
    session.flush()
    return workspace


@pytest.fixture
def two_workspaces(db_session: Session, fixed_clock: FixedClock) -> tuple[Any, Any]:
    first = _workspace(db_session, "Workspace A")
    second = _workspace(db_session, "Workspace B")
    return context_for(first.id, fixed_clock), context_for(second.id, fixed_clock)


OWNED_MODELS = owned_models()


# Discovery and registries


def test_discovery_finds_every_workspace_owned_model_with_repository_and_factory() -> None:
    assert {Membership, AuditEvent} <= set(OWNED_MODELS)
    assert discovery_problems(Base, repositories_for(Base), TENANT_CASES) == []


@pytest.mark.parametrize("model", OWNED_MODELS, ids=lambda model: model.__name__)
def test_factories_build_rows_for_the_requested_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any], model: type[Any]
) -> None:
    for ctx in two_workspaces:
        row = TENANT_CASES[model].factory(db_session, ctx.workspace.id)
        assert type(row) is model
        assert row.workspace_id == ctx.workspace.id
        repositories_for(Base)[model](db_session, ctx).add(row)
        stored = raw_row(db_session, model, row.id)
        assert stored is not None and stored["workspace_id"] == ctx.workspace.id


# The cross-tenant check


@pytest.mark.parametrize("model", OWNED_MODELS, ids=lambda model: model.__name__)
def test_user_b_cannot_read_update_or_delete_user_a_rows(
    db_session: Session, two_workspaces: tuple[Any, Any], model: type[Any]
) -> None:
    case = TENANT_CASES[model]
    repository = repositories_for(Base)[model]
    assert repository.unsupported == case.unsupported, "unsupported operations changed"
    ctx_a, ctx_b = two_workspaces
    check_isolation(
        db_session,
        model,
        repositories_for(Base)[model],
        case.factory,
        case.update_values,
        ctx_a,
        ctx_b,
    )


def test_add_refuses_rows_of_another_workspace_or_type(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    foreign = TENANT_CASES[Membership].factory(db_session, ctx_a.workspace.id)
    with pytest.raises(ForeignWorkspace):
        MembershipRepository(db_session, ctx_b).add(foreign)
    assert foreign not in db_session
    with pytest.raises(TypeError):
        MembershipRepository(db_session, ctx_b).add(  # type: ignore[arg-type]
            TENANT_CASES[AuditEvent].factory(db_session, ctx_b.workspace.id)
        )
    unscoped = TENANT_CASES[Membership].factory(db_session, ctx_b.workspace.id)
    unscoped.workspace_id = None
    assert MembershipRepository(db_session, ctx_b).add(unscoped).workspace_id == ctx_b.workspace.id


@pytest.mark.parametrize("model", OWNED_MODELS, ids=lambda model: model.__name__)
def test_moving_a_row_between_workspaces_fails_at_flush(
    db_session: Session, two_workspaces: tuple[Any, Any], model: type[Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    row = repositories_for(Base)[model](db_session, ctx_a).add(
        TENANT_CASES[model].factory(db_session, ctx_a.workspace.id)
    )
    row.workspace_id = ctx_b.workspace.id
    with pytest.raises(WorkspaceReassignmentError):
        db_session.flush()


def test_instance_level_audit_event_cannot_be_moved_into_a_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any], fixed_clock: FixedClock
) -> None:
    event = AuditEvent(
        id=uuid4(),
        actor_type="system",
        action="auth.test",
        entity_type="user",
        ts=fixed_clock.now(),
    )
    db_session.add(event)
    db_session.flush()
    db_session.expire(event, ["workspace_id"])
    event.workspace_id = two_workspaces[0].workspace.id  # NULL -> a workspace
    with pytest.raises(WorkspaceReassignmentError):
        db_session.flush()


def test_instance_audit_events_are_invisible_to_workspace_repositories(
    db_session: Session, two_workspaces: tuple[Any, Any], fixed_clock: FixedClock
) -> None:
    instance_event = AuditEvent(
        id=uuid4(),
        actor_type="system",
        action="auth.test",
        entity_type="user",
        ts=fixed_clock.now(),
    )
    db_session.add(instance_event)
    db_session.flush()
    for ctx in two_workspaces:
        repository = AuditEventRepository(db_session, ctx)
        assert repository.get(instance_event.id) is None
        assert instance_event.id not in {event.id for event in repository.list()}


def test_audit_events_cannot_be_changed_at_flush(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx = two_workspaces[0]
    repository = AuditEventRepository(db_session, ctx)
    event = repository.add(TENANT_CASES[AuditEvent].factory(db_session, ctx.workspace.id))
    db_session.expire_all()
    loaded = repository.get_or_raise(event.id)
    loaded.action = "test.tampered"
    with pytest.raises(AppendOnlyViolationError, match="cannot be changed"):
        db_session.flush()


def test_audit_events_cannot_be_deleted_at_flush(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx = two_workspaces[0]
    repository = AuditEventRepository(db_session, ctx)
    event = repository.add(TENANT_CASES[AuditEvent].factory(db_session, ctx.workspace.id))
    db_session.expire_all()
    db_session.delete(repository.get_or_raise(event.id))
    with pytest.raises(AppendOnlyViolationError, match="cannot be deleted"):
        db_session.flush()


def test_user_membership_reader_returns_only_its_users_rows(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    alice = User(email="alice@example.com", display_name="Alice")
    bob = User(email="bob@example.com", display_name="Bob")
    db_session.add_all([alice, bob])
    db_session.flush()
    db_session.add_all(
        [
            Membership(workspace_id=ctx_a.workspace.id, user_id=alice.id, role="owner"),
            Membership(workspace_id=ctx_b.workspace.id, user_id=bob.id, role="owner"),
        ]
    )
    db_session.flush()
    alice_reader = UserMembershipReader(db_session, alice.id)
    bob_reader = UserMembershipReader(db_session, bob.id)
    assert [m.workspace_id for m in alice_reader.memberships()] == [ctx_a.workspace.id]
    assert [m.workspace_id for m in bob_reader.memberships()] == [ctx_b.workspace.id]
    assert alice_reader.membership_in(ctx_a.workspace.id) is not None
    assert alice_reader.membership_in(ctx_b.workspace.id) is None
    assert bob_reader.membership_in(ctx_a.workspace.id) is None


def _member(session: Session, workspace_id: UUID, email: str, role: str) -> Membership:
    user = User(email=email, display_name=email.split("@")[0])
    session.add(user)
    session.flush()
    membership = Membership(workspace_id=workspace_id, user_id=user.id, role=role)
    session.add(membership)
    session.flush()
    return membership


def test_membership_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    a_owner = _member(db_session, ctx_a.workspace.id, "a-owner@example.com", "owner")
    a_viewer = _member(db_session, ctx_a.workspace.id, "a-viewer@example.com", "viewer")
    b_owner = _member(db_session, ctx_b.workspace.id, "b-owner@example.com", "owner")
    b_owner_two = _member(db_session, ctx_b.workspace.id, "b-owner2@example.com", "owner")
    in_a, in_b = MembershipRepository(db_session, ctx_a), MembershipRepository(db_session, ctx_b)

    assert {m.membership_id for m in in_a.members()} == {a_owner.id, a_viewer.id}
    assert {m.membership_id for m in in_b.members()} == {b_owner.id, b_owner_two.id}
    assert in_a.for_user(a_owner.user_id) is a_owner
    assert in_a.for_user(b_owner.user_id) is None
    assert in_b.for_user(a_viewer.user_id) is None
    assert (in_a.count_owners(), in_b.count_owners()) == (1, 2)

    newcomer = User(email="newcomer@example.com", display_name="Newcomer")
    db_session.add(newcomer)
    db_session.flush()
    added = in_a.add_member(user_id=newcomer.id, role="editor")
    assert added.workspace_id == ctx_a.workspace.id
    assert in_b.for_user(newcomer.id) is None


def test_audit_record_writes_into_its_own_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any], fixed_clock: FixedClock
) -> None:
    ctx_a, ctx_b = two_workspaces
    actor = _member(db_session, ctx_a.workspace.id, "actor@example.com", "owner")
    event = AuditEventRepository(db_session, ctx_a).record(
        actor_user_id=actor.user_id,
        action="test.recorded",
        entity_type="test",
        entity_id=None,
        ts=fixed_clock.now(),
        data={"n": 1},
    )
    stored = raw_row(db_session, AuditEvent, event.id)
    assert stored is not None and stored["workspace_id"] == ctx_a.workspace.id
    assert AuditEventRepository(db_session, ctx_b).get(event.id) is None


def test_audit_pages_stay_in_their_scope(
    db_session: Session, two_workspaces: tuple[Any, Any], fixed_clock: FixedClock
) -> None:
    ctx_a, ctx_b = two_workspaces
    actor = _member(db_session, ctx_a.workspace.id, "actor@example.com", "owner")
    for ctx, action in ((ctx_a, "test.in_a"), (ctx_b, "test.in_b")):
        AuditEventRepository(db_session, ctx).record(
            actor_user_id=actor.user_id,
            action=action,
            entity_type="test",
            entity_id=None,
            ts=fixed_clock.now(),
        )
    db_session.add(
        AuditEvent(
            id=uuid4(),
            workspace_id=None,
            actor_type="system",
            action="test.instance",
            entity_type="test",
            data={},
            ts=fixed_clock.now(),
        )
    )
    db_session.flush()
    everything = AuditFilter()
    assert [
        e.action for e in AuditEventRepository(db_session, ctx_a).page(everything, limit=10)
    ] == ["test.in_a"]
    assert [
        e.action for e in AuditEventRepository(db_session, ctx_b).page(everything, limit=10)
    ] == ["test.in_b"]
    # The instance reader never returns a workspace's events, whatever the filter.
    assert [e.action for e in InstanceAuditReader(db_session).page(everything, limit=10)] == [
        "test.instance"
    ]
    asked_for_a = AuditFilter(actions=("test.in_a", "test.in_b"))
    assert InstanceAuditReader(db_session).page(asked_for_a, limit=10) == []


def test_current_workspace_repository_touches_only_its_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    a_repo = CurrentWorkspaceRepository(db_session, ctx_a)
    b_repo = CurrentWorkspaceRepository(db_session, ctx_b)
    assert a_repo.get().id == ctx_a.workspace.id
    assert b_repo.get().id == ctx_b.workspace.id
    before_b = raw_row(db_session, Workspace, ctx_b.workspace.id)

    a_repo.update(name="Renamed A", base_currency="PLN")
    db_session.expire_all()
    after_a = raw_row(db_session, Workspace, ctx_a.workspace.id)
    assert after_a is not None and (after_a["name"], after_a["base_currency"]) == (
        "Renamed A",
        "PLN",
    )
    assert raw_row(db_session, Workspace, ctx_b.workspace.id) == before_b

    assert {"name", "base_currency", "country_code", "timezone", "locale"} == UPDATABLE_FIELDS
    for forbidden in ("id", "created_at", "updated_at", "unknown"):
        with pytest.raises(ValueError):
            a_repo.update(**{forbidden: ctx_b.workspace.id})
    db_session.expire_all()
    assert raw_row(db_session, Workspace, ctx_b.workspace.id) == before_b

    # lock (F2.5) reads only its own workspace's row, and refuses a workspace that is gone.
    a_repo.lock(exclusive=False)
    b_repo.lock(exclusive=True)
    gone = CurrentWorkspaceRepository(
        db_session, context_for(uuid4(), FixedClock("2026-01-01T00:00:00+00:00"))
    )
    with pytest.raises(NotFound):
        gone.lock(exclusive=False)
    assert raw_row(db_session, Workspace, ctx_b.workspace.id) == before_b


def test_invitation_transitions_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any], fixed_clock: FixedClock
) -> None:
    ctx_a, ctx_b = two_workspaces
    now = fixed_clock.now()
    creator = _member(db_session, ctx_a.workspace.id, "creator@example.com", "owner")
    in_a, in_b = InvitationRepository(db_session, ctx_a), InvitationRepository(db_session, ctx_b)

    def make(repository: InvitationRepository) -> Invitation:
        return repository.create(
            token_hash=uuid4().hex * 2,
            role="viewer",
            email=None,
            expires_at=now + timedelta(days=1),
            created_by_user_id=creator.user_id,
        )

    a_invite, b_invite = make(in_a), make(in_b)
    assert a_invite.workspace_id == ctx_a.workspace.id
    assert [i.id for i in in_a.list_pending(now)] == [a_invite.id]
    assert [i.id for i in in_b.list_pending(now)] == [b_invite.id]
    assert (in_a.count_pending(now), in_b.count_pending(now)) == (1, 1)
    before = raw_row(db_session, Invitation, a_invite.id)
    for transition in (
        lambda: in_b.revoke(a_invite.id, now),
        lambda: in_b.mark_used(a_invite.id, user_id=creator.user_id, now=now),
        lambda: in_b.record_failed_email(a_invite.id, now=now, limit=5),
    ):
        with pytest.raises(NotFound):
            transition()
    db_session.expire_all()
    assert raw_row(db_session, Invitation, a_invite.id) == before
    # Positive control: the owning workspace can make each transition.
    assert in_a.record_failed_email(a_invite.id, now=now, limit=2) is False
    assert in_a.record_failed_email(a_invite.id, now=now, limit=2) is True
    assert in_a.count_pending(now) == 0
    in_b.mark_used(b_invite.id, user_id=creator.user_id, now=now)
    with pytest.raises(NotFound):
        in_b.revoke(b_invite.id, now)  # only pending invitations can be revoked


def test_invitation_token_reader_finds_only_the_matching_hash(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    a_invite = InvitationRepository(db_session, ctx_a).add(
        TENANT_CASES[Invitation].factory(db_session, ctx_a.workspace.id)
    )
    b_invite = InvitationRepository(db_session, ctx_b).add(
        TENANT_CASES[Invitation].factory(db_session, ctx_b.workspace.id)
    )
    reader = InvitationTokenReader(db_session)
    found = reader.locate(a_invite.token_hash)
    assert found is not None
    assert (found.invitation_id, found.workspace_id) == (a_invite.id, ctx_a.workspace.id)
    found_b = reader.locate(b_invite.token_hash)
    assert found_b is not None and found_b.workspace_id == ctx_b.workspace.id
    assert reader.locate("0" * 64) is None
    assert reader.locate(a_invite.token_hash.upper()) is None


def test_category_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    in_a, in_b = CategoryRepository(db_session, ctx_a), CategoryRepository(db_session, ctx_b)

    def make(repository: CategoryRepository, name: str, kind: str = "expense") -> Any:
        return repository.create(
            name=name,
            name_key=name.casefold(),
            kind=kind,
            treatment="regular",
            color="#16a34a",
            sort_order=0,
        )

    a_food, a_salary = make(in_a, "Food"), make(in_a, "Salary", "income")
    b_food = make(in_b, "Food")  # the same name in another workspace is no clash
    assert (a_food.workspace_id, b_food.workspace_id) == (ctx_a.workspace.id, ctx_b.workspace.id)
    assert [c.id for c in in_a.ordered()] == [a_food.id, a_salary.id]
    assert [c.id for c in in_a.ordered("income")] == [a_salary.id]
    assert [c.id for c in in_b.ordered()] == [b_food.id]
    assert in_b.ordered("income") == []
    assert in_a.active_by_name_key(kind="expense", name_key="food") is a_food
    assert in_b.active_by_name_key(kind="expense", name_key="food") is b_food
    assert in_b.active_by_name_key(kind="income", name_key="salary") is None
    assert (in_a.count(), in_b.count()) == (2, 1)


def test_month_state_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    in_a, in_b = MonthStateRepository(db_session, ctx_a), MonthStateRepository(db_session, ctx_b)
    october, november = YearMonth(2026, 10), YearMonth(2026, 11)

    a_october = in_a.create(october)
    assert (a_october.workspace_id, a_october.month) == (ctx_a.workspace.id, date(2026, 10, 1))
    assert in_a.for_month(october) is a_october
    assert in_b.for_month(october) is None  # another workspace's month is not seen
    assert in_a.for_month(november) is None
    b_october = in_b.create(october)  # the same month in another workspace is no clash
    assert b_october.workspace_id == ctx_b.workspace.id
    assert in_b.for_month(october) is b_october
    assert in_a.for_month(october) is a_october
    in_a.update(a_october.id, closed_at=datetime(2026, 10, 31, 12, tzinfo=UTC))
    assert in_a.closed_months() == {october}
    assert in_b.closed_months() == frozenset()  # B's October is open; A's close is not seen


def test_transaction_queries_stay_in_their_workspace(
    db_session: Session, two_workspaces: tuple[Any, Any]
) -> None:
    ctx_a, ctx_b = two_workspaces
    in_a, in_b = TransactionRepository(db_session, ctx_a), TransactionRepository(db_session, ctx_b)
    october = YearMonth(2026, 10)
    assert (in_a.any_exists(), in_b.any_exists()) == (False, False)

    def category(ctx: Any, name: str) -> Any:
        return CategoryRepository(db_session, ctx).create(
            name=name, name_key=name.casefold(), kind="expense", treatment="regular",
            color="#16a34a", sort_order=0,
        )  # fmt: skip

    def make(repository: TransactionRepository, category_id: UUID, description: str) -> Any:
        return repository.create(
            direction="expense", day=date(2026, 10, 7), amount_minor=1250, currency="EUR",
            category_id=category_id, description=description,
            description_key=description.casefold(), source="manual", created_by_user_id=None,
        )  # fmt: skip

    food_a, food_b = category(ctx_a, "Food"), category(ctx_b, "Food")
    a_row = make(in_a, food_a.id, "Lidl")
    assert a_row.workspace_id == ctx_a.workspace.id
    assert (in_a.any_exists(), in_b.any_exists()) == (True, False)
    assert [r.id for r in in_a.for_month(october)] == [a_row.id]
    assert in_b.for_month(october) == []
    assert in_a.get_for_update(a_row.id) is a_row
    assert in_b.get_for_update(a_row.id) is None  # another workspace's row is not found
    assert set(in_a.counts_by_category()) == {food_a.id}
    assert in_b.counts_by_category() == {}
    assert in_a.active_months_by_category() == {food_a.id: frozenset({october})}
    assert in_b.active_months_by_category() == {}
    assert in_a.description_keys(direction="expense", key_prefix="li", limit=10) == ["lidl"]
    assert in_b.description_keys(direction="expense", key_prefix="li", limit=10) == []
    assert [m.description for m in in_a.description_details(
        direction="expense", keys=["lidl"])] == ["Lidl"]  # fmt: skip
    assert in_b.description_details(direction="expense", keys=["lidl"]) == []
    b_row = make(in_b, food_b.id, "Lidl")
    assert b_row.workspace_id == ctx_b.workspace.id
    assert [r.id for r in in_b.for_month(october)] == [b_row.id]
    assert [r.id for r in in_a.for_month(october)] == [a_row.id]


# Method coverage (#11)


def test_every_public_repository_method_is_covered() -> None:
    module = sys.modules[__name__]
    repositories = list(repositories_for(Base).values())
    assert uncovered_methods(repositories, BASE_METHOD_COVERAGE, EXTRA_METHOD_COVERAGE) == []
    assert set(BASE_METHOD_COVERAGE) == public_methods(ScopedRepository), "stale base entry"
    names = {repository.__name__: repository for repository in repositories}
    for repository_name, method in EXTRA_METHOD_COVERAGE:
        assert method in public_methods(names[repository_name]), "stale extra entry"
    assert set(READER_METHOD_COVERAGE) == public_methods(UserMembershipReader)
    assert set(CURRENT_WORKSPACE_METHOD_COVERAGE) == public_methods(CurrentWorkspaceRepository)
    assert set(TOKEN_READER_METHOD_COVERAGE) == public_methods(InvitationTokenReader)
    assert set(INSTANCE_AUDIT_READER_METHOD_COVERAGE) == public_methods(InstanceAuditReader)
    covering = {
        *INSTANCE_AUDIT_READER_METHOD_COVERAGE.values(),
        *TOKEN_READER_METHOD_COVERAGE.values(),
        *BASE_METHOD_COVERAGE.values(),
        *EXTRA_METHOD_COVERAGE.values(),
        *READER_METHOD_COVERAGE.values(),
        *CURRENT_WORKSPACE_METHOD_COVERAGE.values(),
    }
    for test_name in covering:
        assert callable(getattr(module, test_name, None)), f"no test named {test_name}"


# Composite foreign keys between workspace-owned tables


def test_workspace_owned_foreign_keys_include_workspace_id() -> None:
    assert non_composite_workspace_fks(Base) == []


# Self-tests: the same checks on a scratch base with deliberately broken models (#17)


class ScratchBase(DeclarativeBase):
    metadata = MetaData(naming_convention=CONSTRAINT_NAMING_CONVENTION)


class ScratchWorkspace(IdMixin, TimestampMixin, ScratchBase):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(80), nullable=False)


class ScratchNote(IdMixin, WorkspaceScopedMixin, ScratchBase):
    __tablename__ = "scratch_notes"

    text: Mapped[str] = mapped_column(String(80), nullable=False)


class ScratchUnmarked(IdMixin, ScratchBase):
    """Has workspace_id but not the WorkspaceOwned marker."""

    __tablename__ = "scratch_unmarked"

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class ScratchChild(IdMixin, WorkspaceScopedMixin, ScratchBase):
    """A plain FK to another workspace-owned table: must be reported."""

    __tablename__ = "scratch_children"

    note_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    __table_args__ = (ForeignKeyConstraint(["note_id"], ["scratch_notes.id"]),)


class ScratchTag(IdMixin, WorkspaceScopedMixin, ScratchBase):
    __tablename__ = "scratch_tags"

    label: Mapped[str] = mapped_column(String(80), nullable=False)


class ChattyTagRepository(ScopedRepository[ScratchTag]):
    """Adds a public method without an entry in the coverage registry."""

    model = ScratchTag

    def search(self, label: str) -> list[ScratchTag]:
        return list(self._session.scalars(self._scoped_select().where(ScratchTag.label == label)))


class LeakyNoteRepository(ScopedRepository[ScratchNote]):
    model = ScratchNote

    def _scoped_select(self) -> Any:  # forgets the workspace filter
        from sqlalchemy import select

        return select(ScratchNote)


class ScratchLetter(IdMixin, WorkspaceScopedMixin, ScratchBase):
    __tablename__ = "scratch_letters"

    text: Mapped[str] = mapped_column(String(80), nullable=False)


class ScratchMemo(IdMixin, WorkspaceScopedMixin, ScratchBase):
    __tablename__ = "scratch_memos"

    text: Mapped[str] = mapped_column(String(80), nullable=False)


class UnscopedGetRepository(ScopedRepository[ScratchLetter]):
    model = ScratchLetter

    def get(self, row_id: UUID) -> ScratchLetter | None:  # bypasses the workspace filter
        return self._session.get(ScratchLetter, row_id)


class UnscopedDeleteRepository(ScopedRepository[ScratchMemo]):
    model = ScratchMemo

    def delete(self, target: ScratchMemo | UUID) -> None:  # deletes whatever it is given
        row_id = target if isinstance(target, UUID) else target.id
        self._session.delete(self._session.get(ScratchMemo, row_id))
        self._session.flush()


@pytest.fixture
def scratch_session(tmp_path: Path, fixed_clock: FixedClock) -> Iterator[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'scratch.db'}")
    ScratchBase.metadata.create_all(engine)
    try:
        with Session(engine, info={"clock": fixed_clock}) as session:
            yield session
    finally:
        engine.dispose()


def _note(session: Session, workspace_id: UUID) -> ScratchNote:
    return ScratchNote(workspace_id=workspace_id, text="note")


def _tag(session: Session, workspace_id: UUID) -> ScratchTag:
    return ScratchTag(workspace_id=workspace_id, label="tag")


def test_isolation_harness_detects_discovery_problems() -> None:
    cases = {
        model: object()
        for model in (ScratchNote, ScratchTag, ScratchLetter, ScratchMemo, ScratchWorkspace)
    }
    assert discovery_problems(ScratchBase, repositories_for(ScratchBase), cases) == [
        "ScratchUnmarked has workspace_id but not the WorkspaceOwned marker",
        f"ScratchChild has no repository; {ARCHITECTURE_HINT}",
        f"ScratchChild has no tenant factory; {ARCHITECTURE_HINT}",
        f"ScratchUnmarked has no repository; {ARCHITECTURE_HINT}",
        f"ScratchUnmarked has no tenant factory; {ARCHITECTURE_HINT}",
        "tenant factory for ScratchWorkspace, which is not workspace-owned",
    ]


def _scratch_contexts(session: Session, clock: FixedClock) -> tuple[Any, Any]:
    first, second = ScratchWorkspace(name="A"), ScratchWorkspace(name="B")
    session.add_all([first, second])
    session.flush()
    return context_for(first.id, clock), context_for(second.id, clock)


def test_isolation_harness_passes_a_correctly_scoped_repository(
    scratch_session: Session, fixed_clock: FixedClock
) -> None:
    ctx_a, ctx_b = _scratch_contexts(scratch_session, fixed_clock)
    check_isolation(
        scratch_session, ScratchTag, ChattyTagRepository, _tag, {"label": "x"}, ctx_a, ctx_b
    )


def test_isolation_harness_detects_unscoped_repository(
    scratch_session: Session, fixed_clock: FixedClock
) -> None:
    ctx_a, ctx_b = _scratch_contexts(scratch_session, fixed_clock)
    with pytest.raises(
        AssertionError, match="ScratchNote: could read a row of another workspace through list"
    ):
        check_isolation(
            scratch_session, ScratchNote, LeakyNoteRepository, _note, {"text": "x"}, ctx_a, ctx_b
        )


@pytest.mark.parametrize(
    ("model", "repository", "message"),
    [
        (
            ScratchLetter,
            UnscopedGetRepository,
            "ScratchLetter: could read a row of another workspace through get",
        ),
        (
            ScratchMemo,
            UnscopedDeleteRepository,
            "ScratchMemo: could change a row of another workspace through delete by id",
        ),
    ],
    ids=["get_without_scope", "delete_without_scope"],
)
def test_isolation_harness_detects_unscoped_methods(
    scratch_session: Session,
    fixed_clock: FixedClock,
    model: type[Any],
    repository: type[ScopedRepository[Any]],
    message: str,
) -> None:
    ctx_a, ctx_b = _scratch_contexts(scratch_session, fixed_clock)

    def factory(_session: Session, workspace_id: UUID) -> Any:
        return model(workspace_id=workspace_id, text="x")

    with pytest.raises(AssertionError, match=message):
        check_isolation(scratch_session, model, repository, factory, {"text": "y"}, ctx_a, ctx_b)


def test_isolation_harness_detects_uncovered_methods() -> None:
    assert uncovered_methods([ChattyTagRepository], BASE_METHOD_COVERAGE, {}) == [
        "ChattyTagRepository.search"
    ]
    covered = {("ChattyTagRepository", "search"): "test_search"}
    assert uncovered_methods([ChattyTagRepository], BASE_METHOD_COVERAGE, covered) == []


def test_isolation_harness_detects_non_composite_foreign_keys() -> None:
    assert non_composite_workspace_fks(ScratchBase) == [
        "scratch_children(note_id) -> scratch_notes"
    ]


def test_composite_foreign_keys_pass_the_check() -> None:
    class CompositeBase(DeclarativeBase):
        pass

    class Parent(IdMixin, WorkspaceOwned, CompositeBase):
        __tablename__ = "parents"

        workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)

    class Child(IdMixin, WorkspaceOwned, CompositeBase):
        __tablename__ = "children"

        workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
        parent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
        __table_args__ = (
            ForeignKeyConstraint(
                ["workspace_id", "parent_id"], ["parents.workspace_id", "parents.id"]
            ),
        )

    assert {Parent, Child} == models_marked_workspace_owned(CompositeBase)
    assert non_composite_workspace_fks(CompositeBase) == []


# Relationships into workspace-owned models


def test_no_relationship_from_unowned_models_into_workspace_owned_models() -> None:
    assert relationships_into_owned_models(Base) == []


def test_relationship_check_detects_a_seeded_relationship() -> None:
    class RelationshipBase(DeclarativeBase):
        pass

    class Home(IdMixin, RelationshipBase):
        __tablename__ = "homes"

        notes: Mapped[list[Note]] = relationship()

    class Note(IdMixin, WorkspaceOwned, RelationshipBase):
        __tablename__ = "notes"

        workspace_id: Mapped[UUID] = mapped_column(ForeignKey("homes.id"), nullable=False)
        home: Mapped[Home] = relationship(viewonly=True)  # owned -> unowned is fine

    assert relationships_into_owned_models(RelationshipBase) == ["Home.notes -> Note"]
