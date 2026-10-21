"""Profile settings, sensitive-action confirmation and linked identities (F1.5).

Every sensitive profile action (link, unlink, sign out everywhere) is confirmed through
``confirm_sensitive``, the single entry point:

* A user with a password always confirms with it: ``confirm_password_with_factory``, the
  primitive behind ``/login/link`` (the ``confirm`` throttle, dummy verify, audited
  failures), unchanged.
* A user without a password (created through OIDC or the proxy) may instead rely on a fresh
  external session: the current session started through OIDC or the proxy at most
  ``FRESH_SESSION`` ago (its ``created_at``, not its last activity). This is weaker: a stolen
  session cookie confirms for those minutes (ADR-0012 amendment).
* Otherwise the action is refused as stale ("sign in again") and nothing is written.

``confirmation_method`` decides which branch applies; pages use it to show the right form.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import ExternalIdentity, User, UserSession
from assetboard.domain.identity import InvalidDisplayName, normalize_display_name
from assetboard.i18n.locale import SUPPORTED_LOCALES
from assetboard.services.auth import (
    Throttled,
    Verifier,
    confirm_password_with_factory,
    record_auth_event,
)
from assetboard.services.external_identity import PROXY_PROVIDER

FRESH_SESSION = timedelta(minutes=10)
THEMES = ("system", "light", "dark")
BROWSER_LOCALE = "browser"  # the profile's "Browser language" choice (stored as NULL)

ConfirmMethod = Literal["password", "fresh_session", "none"]
ConfirmStatus = Literal["confirmed", "failed", "throttled", "stale", "busy"]


@dataclass(frozen=True, slots=True)
class ConfirmOutcome[T]:
    """``failed``: wrong password (audited by the primitive). ``throttled``: refused unchecked
    until ``throttled_until``; the user is signed in, so saying so discloses nothing.
    ``stale``: no password and no fresh external session (nothing written). ``busy``: the
    database was locked (set by the web entry point)."""

    status: ConfirmStatus
    value: T | None = None
    throttled_until: datetime | None = None


def _fresh_external_session(row: UserSession | None, user_id: UUID, now: datetime) -> bool:
    return (
        row is not None
        and row.user_id == user_id
        and row.auth_method in {"oidc", "proxy"}
        and now - row.created_at <= FRESH_SESSION
        and row.created_at <= now
    )


def confirmation_method(
    session: Session, *, user_id: UUID, session_hash: str, now: datetime
) -> ConfirmMethod:
    """How this user confirms a sensitive action right now."""
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        return "none"
    if user.password_hash is not None:
        return "password"
    row = session.get(UserSession, session_hash)
    return "fresh_session" if _fresh_external_session(row, user_id, now) else "none"


def fresh_until(session: Session, session_hash: str) -> datetime | None:
    """When the fresh-session confirmation of this session runs out (for the page)."""
    row = session.get(UserSession, session_hash)
    return row.created_at + FRESH_SESSION if row is not None else None


def confirm_sensitive[T](
    factory: sessionmaker[Session],
    *,
    user_id: UUID,
    session_hash: str,
    password: str,
    purpose: str,
    settings: Settings,
    clock: Clock,
    on_success: Callable[[Session, User, ConfirmMethod], T],
    host: str | None = None,
    user_agent: str | None = None,
    verify: Verifier | None = None,
    second_factor: str | None = None,
) -> ConfirmOutcome[T]:
    """Confirm, then run ``on_success`` in the same write transaction (one entry point)."""
    with factory() as session:
        method = confirmation_method(
            session, user_id=user_id, session_hash=session_hash, now=clock.now()
        )
    if method == "password":
        value = confirm_password_with_factory(
            factory,
            user_id=user_id,
            password=password,
            purpose=purpose,
            settings=settings,
            clock=clock,
            on_success=lambda session, user: on_success(session, user, "password"),
            host=host,
            user_agent=user_agent,
            verify=verify,
            second_factor=second_factor,
        )
        if isinstance(value, Throttled):
            return ConfirmOutcome("throttled", throttled_until=value.until)
        if value is None:
            return ConfirmOutcome("failed")
        return ConfirmOutcome("confirmed", value)
    if method == "none":
        return ConfirmOutcome("stale")

    def fresh(session: Session) -> ConfirmOutcome[T]:
        # Decided again under the write lock: the session may have ended or a password been
        # set in the meantime.
        user = session.get(User, user_id)
        row = session.get(UserSession, session_hash)
        if (
            user is None
            or not user.is_active
            or user.password_hash is not None
            or not _fresh_external_session(row, user_id, clock.now())
        ):
            return ConfirmOutcome("stale")
        return ConfirmOutcome("confirmed", on_success(session, user, "fresh_session"))

    return run_immediate(factory, fresh)


# Profile settings


ProfileError = Literal["display_name", "locale", "theme"]


def update_profile(
    factory: sessionmaker[Session],
    *,
    user_id: UUID,
    display_name: str,
    locale: str,
    theme: str,
) -> ProfileError | None:
    """Display name (``normalize_display_name``), locale and theme.

    ``locale`` is a supported language, or ``""`` / ``"browser"`` for none (stored as NULL:
    the browser's ``Accept-Language`` applies).
    """
    try:
        # The same rule as setup, registration and invitation sign-up.
        name = normalize_display_name(display_name)
    except InvalidDisplayName:
        return "display_name"
    saved_locale: str | None = None if locale in {"", BROWSER_LOCALE} else locale
    if saved_locale is not None and saved_locale not in SUPPORTED_LOCALES:
        return "locale"
    if theme not in THEMES:
        return "theme"

    def save(session: Session) -> None:
        user = session.get(User, user_id)
        if user is not None:
            user.display_name, user.locale, user.theme = name, saved_locale, theme

    run_immediate(factory, save)
    return None


# Linked identities


@dataclass(frozen=True, slots=True)
class LinkedIdentity:
    id: UUID
    kind: Literal["oidc", "proxy"]
    current: bool  # usable with today's settings
    issuer: str | None  # OIDC only; the page shows the configured provider's name instead
    proxy_user: str | None  # proxy only: the user's own proxy user name
    email_at_link: str | None
    linked_at: datetime


def _usable(identity: ExternalIdentity, settings: Settings) -> bool:
    if identity.provider == PROXY_PROVIDER:
        return settings.proxy_auth_enabled
    return settings.oidc_enabled and identity.provider == settings.oidc_issuer


def password_usable(user: User, settings: Settings) -> bool:
    """Whether this user can actually sign in with a password under today's settings."""
    if user.password_hash is None:
        return False
    if not settings.oidc_only:
        return True
    return settings.allow_local_admin_login and user.is_instance_admin


def linked_identities(session: Session, user_id: UUID, settings: Settings) -> list[LinkedIdentity]:
    rows = session.scalars(
        select(ExternalIdentity)
        .where(ExternalIdentity.user_id == user_id)
        .order_by(ExternalIdentity.created_at)
    ).all()
    return [
        LinkedIdentity(
            id=row.id,
            kind="proxy" if row.provider == PROXY_PROVIDER else "oidc",
            current=_usable(row, settings),
            issuer=None if row.provider == PROXY_PROVIDER else row.provider,
            proxy_user=row.subject if row.provider == PROXY_PROVIDER else None,
            email_at_link=row.email_at_link,
            linked_at=row.created_at,
        )
        for row in rows
    ]


def has_current_oidc_identity(session: Session, user_id: UUID, settings: Settings) -> bool:
    if not settings.oidc_enabled:
        return False
    return (
        session.scalar(
            select(ExternalIdentity.id).where(
                ExternalIdentity.user_id == user_id,
                ExternalIdentity.provider == settings.oidc_issuer,
            )
        )
        is not None
    )


LinkStatus = Literal["linked", "already_linked", "refused", "one_per_provider"]


def link_identity_to_user(
    session: Session,
    user: User,
    *,
    provider: str,
    subject: str,
    email: str | None,
    confirmed: ConfirmMethod,
    clock: Clock,
    host: str | None,
    user_agent: str | None,
) -> LinkStatus:
    """Link an identity to a signed-in, confirmed user (inside the confirming transaction).

    An identity linked to someone else is ``refused``, the same answer as any other refusal, so
    the result says nothing about other accounts.
    """
    owner = session.scalar(
        select(ExternalIdentity.user_id).where(
            ExternalIdentity.provider == provider, ExternalIdentity.subject == subject
        )
    )
    if owner == user.id:
        return "already_linked"
    if owner is not None or not user.is_active:
        return "refused"
    # One identity per provider: the configured OIDC issuer, or the proxy. Identities of an
    # earlier issuer have a different provider string, so they never count here.
    same_provider = session.scalar(
        select(ExternalIdentity.id).where(
            ExternalIdentity.user_id == user.id, ExternalIdentity.provider == provider
        )
    )
    if same_provider is not None:
        return "one_per_provider"
    session.add(
        ExternalIdentity(user_id=user.id, provider=provider, subject=subject, email_at_link=email)
    )
    session.flush()
    record_auth_event(
        session,
        clock,
        action="auth.proxy_linked" if provider == PROXY_PROVIDER else "auth.oidc_linked",
        entity_id=str(user.id),
        actor_user_id=user.id,
        host=host,
        user_agent=user_agent,
        data={"via": "profile", "confirmed": confirmed},
    )
    return "linked"


UnlinkStatus = Literal["unlinked", "last_method", "not_found"]


def _checkpoint(_name: str) -> None:
    """Test seam: called inside the unlink transaction after the remaining methods were read."""


def unlink_identity(
    session: Session,
    user: User,
    identity_id: UUID,
    *,
    current_session_hash: str,
    confirmed: ConfirmMethod,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None,
) -> UnlinkStatus:
    """Unlink one of the user's identities, unless no way to sign in would remain.

    Runs inside the confirming ``BEGIN IMMEDIATE`` transaction, so two unlinks at once see each
    other. Usable afterwards: a password allowed by today's settings, or another identity of a
    method that is on (the configured OIDC issuer, or the proxy). A refusal writes nothing.
    """
    identity = session.get(ExternalIdentity, identity_id)
    if identity is None or identity.user_id != user.id:
        return "not_found"
    others = session.scalars(
        select(ExternalIdentity).where(
            ExternalIdentity.user_id == user.id, ExternalIdentity.id != identity.id
        )
    ).all()
    _checkpoint("unlink")
    if not password_usable(user, settings) and not any(_usable(row, settings) for row in others):
        return "last_method"
    is_proxy = identity.provider == PROXY_PROVIDER
    session.delete(identity)
    revoked = 0
    if not is_proxy:
        # Sessions started through OIDC elsewhere end with the link. Proxy sessions end through
        # the gate's binding: their subject no longer belongs to this user.
        revoked = len(
            session.execute(
                delete(UserSession)
                .where(
                    UserSession.user_id == user.id,
                    UserSession.auth_method == "oidc",
                    UserSession.id_hash != current_session_hash,
                )
                .returning(UserSession.id_hash)
            ).all()
        )
    data: dict[str, str | int | bool] = {"via": "profile", "confirmed": confirmed}
    if not is_proxy:
        data["other_sessions_revoked"] = revoked
    record_auth_event(
        session,
        clock,
        action="auth.proxy_unlinked" if is_proxy else "auth.oidc_unlinked",
        entity_id=str(user.id),
        actor_user_id=user.id,
        host=host,
        user_agent=user_agent,
        data=data,
    )
    return "unlinked"
