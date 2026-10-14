"""Audit view services: scope authorization and what each scope's rows may carry (F1.5 C)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from assetboard.db.models.identity import AuditEvent, SetupState, User
from assetboard.domain.categories import Kind, Treatment
from assetboard.i18n import gettext
from assetboard.services.admin_users import NotInstanceAdmin
from assetboard.services.audit_view import (
    ACTION_LABELS,
    DATA_KEYS,
    GROUP_LABELS,
    GROUPS,
    KEY_LABELS,
    VALUE_TABLES,
    AuditQuery,
    BadAuditRequest,
    decode_cursor,
    instance_page,
    workspace_page,
)
from assetboard.services.context import PermissionDenied
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for database in workspace_db(tmp_path):
        with database.factory.begin() as session:
            session.execute(update(SetupState).values(completed_at=database.clock.now()))
        yield database


def plant(db: WorkspaceDb, *, workspace_id: UUID | None, actor: UUID, entity: str) -> None:
    with db.factory.begin() as session:
        session.add(
            AuditEvent(
                id=uuid4(),
                workspace_id=workspace_id,
                actor_user_id=actor,
                actor_type="user",
                action="member.removed" if workspace_id else "admin.user_deactivated",
                entity_type="user",
                entity_id=entity,
                data={"role": "viewer", "sessions_revoked": 1},
                ts=db.clock.now(),
                ip_prefix="203.0.113.0/24",
            )
        )


def test_workspace_rows_carry_names_only_no_ip_or_emails(db: WorkspaceDb) -> None:
    db.add_member("olga", "owner")
    db.add_member("kim", "viewer")
    plant(db, workspace_id=db.workspace_id, actor=db.users["olga"], entity=str(db.users["kim"]))
    page = workspace_page(db.factory, db.ctx("olga", "owner"), AuditQuery(scope="workspace"))
    (row,) = page.rows
    assert row.actor_name == "Olga" and row.target_name == "Kim"  # display names only
    assert (row.ip_prefix, row.actor_email, row.target_email) == (None, None, None)
    assert row.details == (("role", "viewer"),)


def test_instance_rows_carry_ip_emails_and_the_affected_account(db: WorkspaceDb) -> None:
    db.add_member("olga", "owner")
    db.add_member("kim", "viewer")
    with db.factory.begin() as session:
        session.execute(
            update(User).where(User.id == db.users["olga"]).values(is_instance_admin=True)
        )
    plant(db, workspace_id=None, actor=db.users["olga"], entity=str(db.users["kim"]))
    page = instance_page(db.factory, actor_id=db.users["olga"], query=AuditQuery(scope="instance"))
    (row,) = page.rows
    assert row.ip_prefix == "203.0.113.0/24"
    assert row.actor_email is not None and row.target_email is not None
    assert row.details == (("sessions_revoked", 1),)


def test_the_services_check_the_scope_themselves(db: WorkspaceDb) -> None:
    db.add_member("kim", "viewer")
    with pytest.raises(PermissionDenied):
        workspace_page(db.factory, db.ctx("kim", "viewer"), AuditQuery(scope="workspace"))
    with pytest.raises(NotInstanceAdmin):
        instance_page(db.factory, actor_id=db.users["kim"], query=AuditQuery(scope="instance"))


@pytest.mark.parametrize(
    "value",
    ["", "abc", "1.2", "-1.0123456789abcdef0123456789abcdef", "1.0123456789ABCDEF0123456789ABCDEF"],
)
def test_malformed_cursors_are_refused(value: str) -> None:
    with pytest.raises(BadAuditRequest):
        decode_cursor(value)


# Category events (F2.1): an action without labels would read "Other event"

CATEGORY_ACTIONS = {
    "category.created",
    "category.updated",
    "category.archived",
    "category.restored",
    "category.deleted",
    "category.seeded",
}


def test_every_category_action_has_a_label_keys_and_a_group() -> None:
    assert {a for a in ACTION_LABELS if a.startswith("category.")} == CATEGORY_ACTIONS
    assert set(GROUPS["workspace"]["categories"]) == CATEGORY_ACTIONS
    assert "categories" in GROUP_LABELS
    keys = {key for action in CATEGORY_ACTIONS for key in DATA_KEYS[action]}
    assert keys <= set(KEY_LABELS)
    assert set(VALUE_TABLES["kind"]) == {k.value for k in Kind}
    for table in ("treatment", "previous_treatment"):
        assert set(VALUE_TABLES[table]) == {t.value for t in Treatment}
    texts = [ACTION_LABELS[a] for a in CATEGORY_ACTIONS] + [KEY_LABELS[k] for k in keys]
    texts += [*VALUE_TABLES["kind"].values(), *VALUE_TABLES["treatment"].values()]
    for text in texts:
        assert gettext(text, "pl") != text, f"no Polish translation for {text!r}"


# Month events (F2.5)

MONTH_ACTIONS = {"month.closed", "month.reopened"}


def test_every_month_action_has_a_label_keys_and_a_group() -> None:
    assert {a for a in ACTION_LABELS if a.startswith("month.")} == MONTH_ACTIONS
    assert set(GROUPS["workspace"]["months"]) == MONTH_ACTIONS
    keys = {key for action in MONTH_ACTIONS for key in DATA_KEYS[action]}
    assert keys == {"month"} and keys <= set(KEY_LABELS)
    texts = [ACTION_LABELS[a] for a in MONTH_ACTIONS] + [KEY_LABELS["month"]]
    texts.append(GROUP_LABELS["months"])
    for text in texts:
        assert gettext(text, "pl") != text, f"no Polish translation for {text!r}"
