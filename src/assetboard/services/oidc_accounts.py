"""Map a validated OIDC identity to a user and sign them in (F1.3, ADR-0014).

The order is fixed, and every step after the first needs what the earlier ones refused:

1. Domain allow-list (``OIDC_ALLOWED_DOMAINS``): a verified email in a listed domain, checked
   on every sign-in, linked identities included.
2. ``ExternalIdentity(issuer, subject)``: that user signs in (refused if inactive).
   From here on the email must be verified ("email unverified" otherwise, before any local
   account is looked at, so an unverified claim reveals nothing).
3. A user with the same email exists. With ``OIDC_LINK_BY_EMAIL``, and only if that user has a
   password, the visitor must confirm the password (``link_identity``). Otherwise: "an account
   with this email exists".
4. ``next`` is an invitation: an account is created and joins that workspace (even without
   ``OIDC_AUTO_CREATE``), under the invitation's email lock and the registration throttle.
5. ``OIDC_AUTO_CREATE``: an account with its own workspace, under the registration throttle.
6. Otherwise: "no account".

Every refusal is audited as
``auth.oidc_failure`` with a reason code and nothing from the provider.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

import anyio.to_thread
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from assetboard.branding import APP_NAME
from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import ExternalIdentity, User, UserSession
from assetboard.services import invitations
from assetboard.services.auth import (
    Verifier,
    confirm_password_with_factory,
    create_passwordless_user,
    create_user_session,
    gate_verifier,
    record_auth_event,
    reserve_registration_attempt,
    revoke_all_sessions,
    session_id_hash,
)
from assetboard.services.hash_gate import HashGate
from assetboard.services.oidc import OidcIdentity
from assetboard.services.oidc_flow import PendingLink

logger = logging.getLogger(f"{APP_NAME.lower()}.oidc")

SignInStatus = Literal[
    "signed_in",
    "confirm_link",
    "account_exists",
    "no_account",
    "email_unverified",
    "domain_not_allowed",
    "inactive",
    "invitation_invalid",
    "email_mismatch",
    "throttled",
]


@dataclass(frozen=True, slots=True)
class OidcSignIn:
    status: SignInStatus
    user_id: UUID | None = None
    session_id: str | None = None
    expires_at: datetime | None = None
    joined_workspace_id: UUID | None = None
    pending_link: PendingLink | None = None
    invitation_revoked: bool = False
    created: bool = False


@dataclass(frozen=True, slots=True)
class _Request:
    settings: Settings
    clock: Clock
    host: str | None
    user_agent: str | None
    existing_session_id: str | None


def invitation_token(next_path: str) -> str | None:
    """The token when ``next`` is exactly an invitation page (``/invite/<token>``)."""
    prefix = "/invite/"
    if not next_path.startswith(prefix):
        return None
    token = next_path.removeprefix(prefix)
    return token if invitations.TOKEN_PATTERN.fullmatch(token) else None


def domain_allowed(identity: OidcIdentity, settings: Settings) -> bool:
    if not settings.oidc_allowed_domains:
        return True
    if identity.email is None or not identity.email_verified:
        return False
    return identity.email.rsplit("@", 1)[1] in settings.oidc_allowed_domains


def admin_decision(identity: OidcIdentity, settings: Settings) -> bool | None:
    """True/False from the groups claim; None (no change) when unmapped or the claim unusable.

    A present claim is authoritative, even an empty list.
    """
    if settings.oidc_admin_group is None or identity.groups is None:
        return None
    return settings.oidc_admin_group in identity.groups


def record_oidc_failure(
    factory: sessionmaker[Session],
    reason: str,
    *,
    clock: Clock,
    host: str | None,
    user_agent: str | None,
    user_id: UUID | None = None,
) -> None:
    """Audit a refused OIDC sign-in: a reason code only, never tokens, codes or claims."""
    run_immediate(
        factory,
        lambda session: _audit_failure(session, reason, clock, host, user_agent, user_id),
    )


def _audit_failure(
    session: Session,
    reason: str,
    clock: Clock,
    host: str | None,
    user_agent: str | None,
    user_id: UUID | None,
) -> None:
    record_auth_event(
        session,
        clock,
        action="auth.oidc_failure",
        entity_id=str(user_id) if user_id else None,
        host=host,
        user_agent=user_agent,
        data={"reason": reason},
    )


def _sync_admin(session: Session, user: User, admin: bool | None, request: _Request) -> None:
    if admin is None or user.is_instance_admin == admin:
        return
    if not admin:
        others = session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.is_instance_admin.is_(True), User.is_active.is_(True), User.id != user.id)
        )
        if not others:
            logger.warning(
                "OIDC admin group says to demote the last active instance administrator; "
                "keeping their admin status"
            )
            return
    user.is_instance_admin = admin
    record_auth_event(
        session,
        request.clock,
        action="auth.instance_admin_granted" if admin else "auth.instance_admin_revoked",
        entity_id=str(user.id),
        host=request.host,
        user_agent=request.user_agent,
        data={"source": "oidc_group"},
    )


def _start_session(
    session: Session, user: User, request: _Request, *, data: dict[str, str | int | bool]
) -> tuple[str, datetime]:
    """A new session id every time; a session the browser already had is ended (rotation)."""
    now = request.clock.now()
    if request.existing_session_id:
        session.execute(
            delete(UserSession).where(
                UserSession.id_hash == session_id_hash(request.existing_session_id)
            )
        )
    session_id = create_user_session(
        session,
        user,
        settings=request.settings,
        clock=request.clock,
        host=request.host,
        user_agent=request.user_agent,
        auth_method="oidc",
    )
    user.last_login_at = now
    record_auth_event(
        session,
        request.clock,
        action="auth.login_success",
        entity_id=str(user.id),
        actor_user_id=user.id,
        host=request.host,
        user_agent=request.user_agent,
        data={"method": "oidc", **data},
    )
    return session_id, now + timedelta(days=request.settings.session_absolute_timeout_days)


def _find_identity(session: Session, issuer: str, subject: str) -> ExternalIdentity | None:
    return session.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.provider == issuer, ExternalIdentity.subject == subject
        )
    )


@dataclass(frozen=True, slots=True)
class _Known:
    identity_user_id: UUID | None
    email_user_id: UUID | None
    email_user_has_password: bool


def sign_in_with_oidc(
    factory: sessionmaker[Session],
    identity: OidcIdentity,
    *,
    next_path: str,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None = None,
    existing_session_id: str | None = None,
) -> OidcSignIn:
    request = _Request(settings, clock, host, user_agent, existing_session_id)
    admin = admin_decision(identity, settings)

    def refuse(
        status: SignInStatus, user_id: UUID | None = None, *, revoked: bool = False
    ) -> OidcSignIn:
        record_oidc_failure(
            factory, status, clock=clock, host=host, user_agent=user_agent, user_id=user_id
        )
        return OidcSignIn(status=status, invitation_revoked=revoked)

    if not domain_allowed(identity, settings):
        return refuse("domain_not_allowed")

    def lookup(session: Session) -> _Known:
        linked = _find_identity(session, identity.issuer, identity.subject)
        by_email = (
            session.scalar(select(User).where(User.email == identity.email))
            if identity.email
            else None
        )
        return _Known(
            linked.user_id if linked else None,
            by_email.id if by_email else None,
            bool(by_email and by_email.password_hash is not None),
        )

    with factory() as session:
        known = lookup(session)

    # 2. A linked identity.
    if known.identity_user_id is not None:
        user_id = known.identity_user_id

        def sign_in(session: Session) -> OidcSignIn:
            user = session.get(User, user_id)
            if user is None or not user.is_active:
                _audit_failure(session, "inactive", clock, host, user_agent, user_id)
                return OidcSignIn(status="inactive")
            _sync_admin(session, user, admin, request)
            session_id, expires = _start_session(session, user, request, data={})
            return OidcSignIn("signed_in", user.id, session_id, expires)

        return run_immediate(factory, sign_in)

    # Everything below needs a verified email. Checked before looking at local accounts, so an
    # unverified claim learns nothing about which addresses are registered.
    if identity.email is None or not identity.email_verified:
        return refuse("email_unverified")
    email = identity.email

    # 3. The email belongs to an existing account.
    if known.email_user_id is not None:
        if settings.oidc_link_by_email and known.email_user_has_password:
            return OidcSignIn(
                status="confirm_link",
                pending_link=PendingLink(
                    user_id=known.email_user_id,
                    issuer=identity.issuer,
                    subject=identity.subject,
                    email=email,
                    admin=admin,
                    next_path=next_path,
                ),
            )
        return refuse("account_exists", known.email_user_id)

    # 4 and 5. Create an account.
    token = invitation_token(next_path)
    if token is None and not settings.oidc_auto_create:
        return refuse("no_account")
    if token is not None:
        try:
            invitations.check_for_sign_up(factory, token, email=email, clock=clock)
        except invitations.EmailMismatch as mismatch:
            # Counted against the invitation (5 revoke it), exactly as for a local sign-up.
            return refuse("email_mismatch", revoked=mismatch.revoked)
        except invitations.InvitationInvalid:
            return refuse("invitation_invalid")
    if not reserve_registration_attempt(factory, host=host, settings=settings, clock=clock):
        return refuse("throttled")

    def create(session: Session) -> OidcSignIn:
        if session.scalar(select(User.id).where(User.email == email)) is not None:
            return OidcSignIn(status="account_exists")
        try:
            with session.begin_nested():
                user, _workspace = create_passwordless_user(
                    session,
                    email=email,
                    display_name=identity.display_name,
                    own_workspace=token is None,
                    settings=settings,
                    is_admin=False,
                )
                session.add(
                    ExternalIdentity(
                        user_id=user.id,
                        provider=identity.issuer,
                        subject=identity.subject,
                        email_at_link=email,
                    )
                )
                session.flush()
        except IntegrityError:
            # A concurrent sign-in created the same email or identity first.
            return OidcSignIn(status="account_exists")
        joined: UUID | None = None
        if token is not None:
            # Raises InvitationInvalid (rolling everything back) if the link was used meanwhile.
            joined = invitations.consume_for_new_user(
                session, token, user_id=user.id, email=email, now=clock.now()
            )
        _sync_admin(session, user, admin, request)
        record_auth_event(
            session,
            clock,
            action="auth.register",
            entity_id=str(user.id),
            actor_user_id=user.id,
            host=host,
            user_agent=user_agent,
            data={"via": "oidc_invitation" if token is not None else "oidc"},
        )
        session_id, expires = _start_session(session, user, request, data={"created": True})
        return OidcSignIn("signed_in", user.id, session_id, expires, joined, created=True)

    try:
        outcome = run_immediate(factory, create)
    except invitations.InvitationInvalid:
        return refuse("invitation_invalid")
    if outcome.status == "account_exists":
        return refuse("account_exists")
    return outcome


LinkStatus = Literal["linked", "failed", "disabled"]


@dataclass(frozen=True, slots=True)
class LinkOutcome:
    """``failed`` covers every refusal alike (wrong password, throttled, changed meanwhile)."""

    status: LinkStatus
    user_id: UUID | None = None
    session_id: str | None = None
    expires_at: datetime | None = None
    revoked_sessions: int = 0


def link_identity(
    factory: sessionmaker[Session],
    pending: PendingLink,
    password: str,
    *,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None = None,
    existing_session_id: str | None = None,
    verify: Verifier | None = None,
) -> LinkOutcome:
    """Link a verified identity to the account with the same email, after its password.

    The password check is ``confirm_password_with_factory`` (login's throttle and dummy verify).
    On success every other session of that account is revoked: whoever created or used the
    account before (pre-hijacking) is signed out. Then the visitor is signed in.
    """
    if not settings.oidc_link_by_email or pending.issuer != settings.oidc_issuer:
        return LinkOutcome(status="disabled")
    request = _Request(settings, clock, host, user_agent, existing_session_id)

    def link(session: Session, user: User) -> LinkOutcome:
        if user.email != pending.email:
            return LinkOutcome(status="failed")
        existing = _find_identity(session, pending.issuer, pending.subject)
        if existing is not None and existing.user_id != user.id:
            return LinkOutcome(status="failed")
        if existing is None:
            session.add(
                ExternalIdentity(
                    user_id=user.id,
                    provider=pending.issuer,
                    subject=pending.subject,
                    email_at_link=pending.email,
                )
            )
            session.flush()
        revoked = revoke_all_sessions(session, user.id)
        _sync_admin(session, user, pending.admin, request)
        record_auth_event(
            session,
            clock,
            action="auth.oidc_linked",
            entity_id=str(user.id),
            actor_user_id=user.id,
            host=host,
            user_agent=user_agent,
            data={"other_sessions_revoked": revoked},
        )
        session_id, expires = _start_session(session, user, request, data={"linked": True})
        return LinkOutcome("linked", user.id, session_id, expires, revoked)

    outcome = confirm_password_with_factory(
        factory,
        user_id=pending.user_id,
        password=password,
        purpose="oidc_link",
        settings=settings,
        clock=clock,
        on_success=link,
        host=host,
        user_agent=user_agent,
        verify=verify,
    )
    return outcome or LinkOutcome(status="failed")


async def link_identity_async(
    factory: sessionmaker[Session],
    gate: HashGate,
    pending: PendingLink,
    password: str,
    *,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None = None,
    existing_session_id: str | None = None,
) -> LinkOutcome:
    """Web entry point: verifies through the hashing limit (``HashGateBusy`` when saturated)."""
    verify = gate_verifier(gate)
    return await anyio.to_thread.run_sync(
        lambda: link_identity(
            factory,
            pending,
            password,
            settings=settings,
            clock=clock,
            host=host,
            user_agent=user_agent,
            existing_session_id=existing_session_id,
            verify=verify,
        )
    )
