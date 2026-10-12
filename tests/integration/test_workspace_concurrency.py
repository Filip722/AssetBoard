"""Concurrent owner actions, made deterministic with the service's checkpoint seam.

The first writer stops at ``_checkpoint`` inside its ``BEGIN IMMEDIATE`` transaction (after
re-reading its own role). The second writer is started and observed reaching its own
``BEGIN IMMEDIATE``, where SQLite makes it wait for the lock. Then the first writer is released.
The second one must see the first one's committed result.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from assetboard.services import workspaces
from assetboard.services.context import PermissionDenied, Role
from assetboard.services.workspaces import LastOwnerError, change_role
from tests.tenant.serialize import serialize_at_checkpoint
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


def run_serialized(
    db: WorkspaceDb,
    monkeypatch: pytest.MonkeyPatch,
    first: Callable[[], None],
    second: Callable[[], None],
) -> dict[str, BaseException | None]:
    return serialize_at_checkpoint(db.engine, monkeypatch, workspaces, first, second)


def test_concurrent_mutual_demotions_keep_one_owner(
    db: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.add_member("ann", "owner")
    db.add_member("bob", "owner")
    outcomes = run_serialized(
        db,
        monkeypatch,
        lambda: change_role(
            db.factory, db.ctx("ann", "owner"), membership_id=db.memberships["bob"], role="editor"
        ),
        lambda: change_role(
            db.factory, db.ctx("bob", "owner"), membership_id=db.memberships["ann"], role="editor"
        ),
    )
    assert outcomes["first"] is None
    assert isinstance(outcomes["second"], PermissionDenied)  # Bob was demoted meanwhile
    assert db.roles() == {"ann": "owner", "bob": "editor"}


def test_demoted_owner_cannot_promote_with_a_stale_context(
    db: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.add_member("ann", "owner")
    db.add_member("bob", "owner")
    db.add_member("cat", "viewer")
    outcomes = run_serialized(
        db,
        monkeypatch,
        lambda: change_role(
            db.factory, db.ctx("ann", "owner"), membership_id=db.memberships["bob"], role="viewer"
        ),
        lambda: change_role(
            db.factory, db.ctx("bob", "owner"), membership_id=db.memberships["cat"], role=Role.OWNER
        ),
    )
    assert outcomes["first"] is None
    assert isinstance(outcomes["second"], PermissionDenied)
    assert db.roles() == {"ann": "owner", "bob": "viewer", "cat": "viewer"}


def test_concurrent_self_demotions_leave_one_owner(
    db: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.add_member("ann", "owner")
    db.add_member("bob", "owner")
    outcomes = run_serialized(
        db,
        monkeypatch,
        lambda: change_role(
            db.factory, db.ctx("ann", "owner"), membership_id=db.memberships["ann"], role="editor"
        ),
        lambda: change_role(
            db.factory, db.ctx("bob", "owner"), membership_id=db.memberships["bob"], role="editor"
        ),
    )
    assert outcomes["first"] is None
    assert isinstance(outcomes["second"], LastOwnerError)
    assert db.roles() == {"ann": "editor", "bob": "owner"}
