from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from assetboard.config import Settings
from assetboard.db.models.identity import Workspace
from assetboard.db.repositories.scheduled import RecurringWorkspaceReader
from assetboard.recurring_jobs import RecurringJobs
from tests.tenant.workspace_db import workspace_db


@pytest.fixture
def db(tmp_path):
    yield from workspace_db(tmp_path)


def test_jobs_follow_workspace_timezones_refresh_and_shutdown(db):
    with db.factory.begin() as session:
        workspace = session.get(Workspace, db.workspace_id)
        assert workspace is not None
        workspace.timezone = "Europe/Warsaw"
    logger = Mock()
    jobs = RecurringJobs(db.factory, Settings(_env_file=None), lambda: db.clock, logger)
    jobs.start()
    try:
        job = jobs.scheduler.get_job(str(db.workspace_id))
        assert job is not None
        fire = job.trigger.get_next_fire_time(None, datetime(2026, 3, 28, 23, tzinfo=UTC))
        assert fire.isoformat() == "2026-03-29T03:00:00+02:00"
        other = db.add_workspace("Other")
        with db.factory.begin() as session:
            workspace = session.get(Workspace, db.workspace_id)
            assert workspace is not None
            workspace.timezone = "America/New_York"
        jobs.reconcile()
        assert jobs.scheduler.get_job(str(other)) is not None
        updated = jobs.scheduler.get_job(str(db.workspace_id))
        assert updated is not None and str(updated.trigger.timezone) == "America/New_York"
        with db.factory.begin() as session:
            assert set(RecurringWorkspaceReader(session).workspaces()) == {
                (db.workspace_id, "America/New_York"),
                (other, "UTC"),
            }
            session.delete(session.get(Workspace, other))
        jobs.reconcile()
        assert jobs.scheduler.get_job(str(other)) is None
        logger.error.assert_not_called()
    finally:
        jobs.stop()
    assert not jobs.scheduler.running


def test_failed_generation_is_logged_without_data_and_later_runs_can_retry(db, monkeypatch):
    logger = Mock()
    jobs = RecurringJobs(db.factory, Settings(_env_file=None), lambda: db.clock, logger)
    generate = Mock(side_effect=[RuntimeError("sensitive SQL parameters"), 0])
    monkeypatch.setattr("assetboard.recurring_jobs.generate_workspace", generate)
    jobs.run(db.workspace_id)
    jobs.run(db.workspace_id)
    assert generate.call_count == 2
    assert logger.error.call_count == 1
    assert "sensitive" not in str(logger.error.call_args)
