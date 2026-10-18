"""The one web entry point for sensitive account actions (F1.5).

Routes call ``run_confirmed`` with the submitted ``confirm_password`` field; it goes through
``services.profile.confirm_sensitive`` (password, or a fresh external session for accounts
without one). ``confirm_view`` tells a page which confirmation to ask for.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import anyio.to_thread
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from assetboard.db.engine import is_database_locked
from assetboard.db.models.identity import User
from assetboard.i18n import gettext
from assetboard.services.auth import WebSession, gate_verifier
from assetboard.services.profile import (
    ConfirmMethod,
    ConfirmOutcome,
    confirm_sensitive,
    confirmation_method,
    fresh_until,
)
from assetboard.web.errors import BUSY_RETRY_AFTER
from assetboard.web.security import client_host
from assetboard.web.throttle import retry_after, throttled_message


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


CONFIRM_FAILED = _("The password is not correct.")
BUSY = _("The server is busy. Try again in a moment.")
CONFIRM_STALE = _(
    "Sign in again to confirm this change: without a password, it needs a sign-in from the "
    "last 10 minutes."
)


@dataclass(frozen=True, slots=True)
class Refusal:
    """A confirmation that did not go through: translated message, status, Retry-After."""

    message: str
    status_code: int
    retry_after: str | None = None


def refusal(request: Request, outcome: ConfirmOutcome[Any]) -> Refusal:
    """One mapping for every page that confirms a sensitive action.

    A throttled confirmation keeps the 401 of a wrong password and adds the wait.
    """
    locale = request.state.locale
    if outcome.status == "busy":
        return Refusal(gettext(BUSY, locale), 503, BUSY_RETRY_AFTER)
    if outcome.status == "stale":
        return Refusal(gettext(CONFIRM_STALE, locale), 403)
    if outcome.status == "throttled":
        now = request.app.state.clock.now()
        until = outcome.throttled_until
        return Refusal(throttled_message(until, now, locale), 401, retry_after(until, now))
    return Refusal(gettext(CONFIRM_FAILED, locale), 401)


@dataclass(frozen=True, slots=True)
class ConfirmView:
    method: ConfirmMethod
    until: datetime | None = None
    minutes_left: int | None = None


async def confirm_view(request: Request, current: WebSession) -> ConfirmView:
    state = request.app.state

    def read() -> ConfirmView:
        with state.session_factory() as session:
            method = confirmation_method(
                session,
                user_id=current.user_id,
                session_hash=current.session_hash,
                now=state.clock.now(),
            )
            until = fresh_until(session, current.session_hash)
        if method != "fresh_session" or until is None:
            return ConfirmView(method)
        seconds = max(0, int((until - state.clock.now()).total_seconds()))
        return ConfirmView(method, until, -(-seconds // 60))

    return await run_in_threadpool(read)


async def run_confirmed[T](
    request: Request,
    current: WebSession,
    *,
    purpose: str,
    password: str,
    action: Callable[[Session, User, ConfirmMethod], T],
) -> ConfirmOutcome[T]:
    """Confirm the signed-in user, then run ``action`` in the same write transaction.

    A locked database is handled here, once, as ``busy`` (the routes answer 503).
    """
    state: Any = request.app.state
    verify = gate_verifier(state.hash_gate)
    try:
        return await _confirm_in_thread(request, current, state, verify, purpose, password, action)
    except OperationalError as exc:
        if not is_database_locked(exc):
            raise
        return ConfirmOutcome("busy")


async def _confirm_in_thread[T](
    request: Request,
    current: WebSession,
    state: Any,
    verify: Any,
    purpose: str,
    password: str,
    action: Callable[[Session, User, ConfirmMethod], T],
) -> ConfirmOutcome[T]:
    return await anyio.to_thread.run_sync(
        lambda: confirm_sensitive(
            state.session_factory,
            user_id=current.user_id,
            session_hash=current.session_hash,
            password=password,
            purpose=purpose,
            settings=state.settings,
            clock=state.clock,
            on_success=action,
            host=client_host(request),
            user_agent=request.headers.get("user-agent"),
            verify=verify,
        )
    )
