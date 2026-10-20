from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.repositories.scheduled import RecurringWorkspaceReader
from assetboard.services.scheduled import generate_workspace


@dataclass(frozen=True, slots=True)
class _Workspace:
    id: UUID


@dataclass(frozen=True, slots=True)
class _Scope:
    workspace: _Workspace


class RecurringJobs:
    def __init__(
        self,
        factory: sessionmaker[Session],
        settings: Settings,
        clock: Callable[[], Clock],
        logger: logging.Logger,
    ) -> None:
        self.factory, self.settings, self.clock, self.logger = factory, settings, clock, logger
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self.zones: dict[UUID, str] = {}

    def run(self, workspace_id: UUID) -> None:
        try:
            generate_workspace(
                self.factory, _Scope(_Workspace(workspace_id)), self.clock(), self.settings
            )
        except Exception:
            self.logger.error(
                "Recurring generation failed for workspace %s; next run will retry", workspace_id
            )

    def reconcile(self) -> None:
        with self.factory() as session:
            workspaces = dict(RecurringWorkspaceReader(session).workspaces())
        for workspace_id, timezone in workspaces.items():
            if self.zones.get(workspace_id) != timezone:
                self.scheduler.add_job(
                    self.run,
                    CronTrigger(hour=3, minute=0, timezone=timezone),
                    args=[workspace_id],
                    id=str(workspace_id),
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=None,
                )
        for workspace_id in self.zones.keys() - workspaces.keys():
            self.scheduler.remove_job(str(workspace_id))
        self.zones = workspaces

    def start(self) -> None:
        self.reconcile()
        for workspace_id in self.zones:
            self.run(workspace_id)
        self.scheduler.add_job(self.reconcile, "interval", minutes=1, id="refresh-workspaces")
        self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=True)
