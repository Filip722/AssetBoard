"""The audit log page (F1.5 part C): two scopes, keyset pages, allowlisted data.

* **Workspace scope** (owner of the current workspace): that workspace's events, read through
  ``AuditEventRepository`` (``workspace_id`` = the current workspace). Actors and affected
  members by display name only ("Former member" when the account no longer resolves).
* **Instance scope** (instance administrator): events with ``workspace_id IS NULL`` only, read
  through ``InstanceAuditReader``: sign-ins, sign-in failures, linking, account administration.
  An instance administrator never sees a workspace's events here. IP prefixes and the affected
  user (display name and email, when ``entity_id`` is a user) appear only in this scope.

``data`` is shown only through ``DATA_KEYS`` (per action); an unknown action shows its time and a
generic label. Nothing else from a row reaches the page: no subjects, tokens or provider emails
are ever stored in audit data, and none could be shown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.models.identity import AuditEvent, User
from assetboard.db.repositories import (
    AuditCursor,
    AuditEventRepository,
    AuditFilter,
    InstanceAuditReader,
)
from assetboard.services.admin_users import AUDIT_DATA_KEYS, require_instance_admin
from assetboard.services.context import Context, Role, require_role

Scope = Literal["workspace", "instance"]
PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


def _(message: str) -> str:
    """Marks a message for extraction; the web layer translates it."""
    return message


# Per-action allowlists of ``data`` keys. Anything not listed is never rendered.
DATA_KEYS: dict[str, tuple[str, ...]] = {
    # Instance scope
    "cli.create_admin": (),
    "cli.reset_password": ("sessions_revoked",),
    "auth.login_success": ("method", "created", "linked"),
    "auth.login_failure": ("reason",),
    "auth.logout": ("scope", "revoked_count"),
    "auth.register": ("via",),
    "auth.password_change": ("other_sessions_revoked",),
    "auth.password_confirm_failure": ("purpose",),
    "auth.oidc_failure": ("reason", "via"),
    "auth.proxy_failure": ("reason",),
    "auth.oidc_linked": ("via", "confirmed", "other_sessions_revoked"),
    "auth.proxy_linked": ("via", "confirmed"),
    "auth.oidc_unlinked": ("via", "confirmed", "other_sessions_revoked"),
    "auth.proxy_unlinked": ("via", "confirmed"),
    "auth.instance_admin_granted": ("source",),
    "auth.instance_admin_revoked": ("source",),
    **AUDIT_DATA_KEYS,
    # Workspace scope
    "setup.create_admin": (),
    "workspace.settings_updated": ("fields",),
    "member.role_changed": ("from", "to"),
    "member.removed": ("role",),
    "invitation.created": ("role", "email_locked", "expires_in_days"),
    "invitation.revoked": ("reason",),
    "invitation.accepted": ("role",),
    "invitation.email_mismatch": (),
    "category.created": ("name", "kind", "treatment"),
    "category.updated": (
        "name",
        "fields",
        "previous_treatment",
        "treatment",
        "affected_transactions",
        "closed_months_affected",
    ),
    "category.archived": ("name", "in_use"),
    "category.restored": ("name",),
    "category.deleted": ("name", "kind"),
    "category.seeded": ("count",),
    "month.closed": ("month",),
    "month.reopened": ("month",),
    # Never the description: audit rows are append-only, free text there could not be erased.
    "transaction.created": ("date", "direction", "category", "amount", "currency"),
    "transaction.updated": (
        "fields",
        "date",
        "previous_date",
        "category",
        "amount",
        "previous_amount",
        "currency",
    ),
    "transaction.deleted": ("date", "direction", "category", "amount", "currency"),
    "transaction.restored": ("date", "direction", "category", "amount", "currency"),
}

ACTION_LABELS: dict[str, str] = {
    "setup.create_admin": _("First administrator created at setup"),
    "cli.create_admin": _("Administrator created on the command line"),
    "cli.reset_password": _("Password reset on the command line"),
    "auth.login_success": _("Signed in"),
    "auth.login_failure": _("Sign-in failed"),
    "auth.logout": _("Signed out"),
    "auth.register": _("Account registered"),
    "auth.password_change": _("Password changed"),
    "auth.password_confirm_failure": _("Password confirmation failed"),
    "auth.oidc_failure": _("Identity provider sign-in refused"),
    "auth.proxy_failure": _("Proxy sign-in refused"),
    "auth.oidc_linked": _("Identity provider linked"),
    "auth.proxy_linked": _("Proxy sign-in linked"),
    "auth.oidc_unlinked": _("Identity provider unlinked"),
    "auth.proxy_unlinked": _("Proxy sign-in unlinked"),
    "auth.instance_admin_granted": _("Administrator role granted"),
    "auth.instance_admin_revoked": _("Administrator role removed"),
    "admin.user_created": _("Account created by an administrator"),
    "admin.user_activated": _("Account activated"),
    "admin.user_deactivated": _("Account deactivated"),
    "admin.admin_granted": _("Administrator role granted"),
    "admin.admin_revoked": _("Administrator role removed"),
    "workspace.settings_updated": _("Workspace settings changed"),
    "member.role_changed": _("Member role changed"),
    "member.removed": _("Member removed"),
    "invitation.created": _("Invitation created"),
    "invitation.revoked": _("Invitation revoked"),
    "invitation.accepted": _("Invitation accepted"),
    "invitation.email_mismatch": _("Invitation used with another email address"),
    "category.created": _("Category added"),
    "category.updated": _("Category changed"),
    "category.archived": _("Category archived"),
    "category.restored": _("Category restored"),
    "category.deleted": _("Category deleted"),
    "category.seeded": _("Suggested categories added"),
    "month.closed": _("Month closed"),
    "month.reopened": _("Month reopened"),
    "transaction.created": _("Transaction added"),
    "transaction.updated": _("Transaction changed"),
    "transaction.deleted": _("Transaction deleted"),
    "transaction.restored": _("Deleted transaction restored"),
}
UNKNOWN_ACTION = _("Other event")
# A failure while linking from the profile is not a refused sign-in.
LINK_FAILURE = _("Linking an identity provider failed")

KEY_LABELS: dict[str, str] = {
    "method": _("Method"),
    "created": _("New account"),
    "linked": _("Just linked"),
    "reason": _("Reason"),
    "scope": _("Scope"),
    "revoked_count": _("Sessions ended"),
    "sessions_revoked": _("Sessions ended"),
    "other_sessions_revoked": _("Other sessions ended"),
    "via": _("Through"),
    "purpose": _("For"),
    "confirmed": _("Confirmed with"),
    "source": _("Source"),
    "temporary_password": _("Temporary password"),
    "fields": _("Fields"),
    "from": _("From"),
    "to": _("To"),
    "role": _("Role"),
    "email_locked": _("Email address fixed"),
    "expires_in_days": _("Valid for days"),
    "name": _("Name"),
    "kind": _("Kind"),
    "treatment": _("Treatment"),
    "previous_treatment": _("Previous treatment"),
    "affected_transactions": _("Transactions affected"),
    "in_use": _("In use"),
    "count": _("Number"),
    "month": _("Month"),
    "closed_months_affected": _("Closed months affected"),
    "date": _("Date"),
    "previous_date": _("Previous date"),
    "direction": _("Direction"),
    "category": _("Category"),
    "amount": _("Amount"),
    "previous_amount": _("Previous amount"),
    "currency": _("Currency"),
}

# Codes stored as values, with their texts, per kind of key: the same code can mean different
# things ("owner" as a role, or as who revoked an invitation). A code without a text in its
# table is shown as the code.
ROLE_LABELS: dict[str, str] = {
    "owner": _("owner"),
    "editor": _("editor"),
    "viewer": _("viewer"),
}
REASON_LABELS: dict[str, str] = {
    "not_instance_admin": _("not an instance administrator"),
    "no_account": _("no account for this sign-in"),
    "account_exists": _("an account with this email address already exists"),
    "email_unverified": _("email address not verified"),
    "domain_not_allowed": _("email domain not allowed"),
    "inactive": _("account deactivated"),
    "invitation_invalid": _("invitation no longer valid"),
    "email_mismatch": _("email address does not match the invitation"),
    "throttled": _("too many attempts"),
    "id_token_invalid": _("the provider's ID token was not valid"),
    "unknown_key": _("the provider signed with an unknown key"),
    "jwks_failed": _("the provider's keys could not be read"),
    "nonce_mismatch": _("the sign-in did not match its request"),
    "userinfo_failed": _("the provider's user information could not be read"),
    "userinfo_sub_mismatch": _("the provider's user information did not match"),
    "pending_link_too_large": _("the sign-in data was too large"),
    "headers_duplicate": _("the proxy sent an identity header twice"),
    "headers_invalid": _("the proxy sent an invalid identity header"),
    "failed_email_attempts": _("too many attempts with another email address"),
    "owner": _("by an owner"),
}
METHOD_LABELS: dict[str, str] = {
    "password": _("password"),
    "oidc": _("identity provider"),
    "proxy": _("proxy"),
    "profile": _("the profile"),
    "invitation": _("an invitation"),
    "oidc_invitation": _("an invitation, through the identity provider"),
    "proxy_invitation": _("an invitation, through the proxy"),
    "fresh_session": _("a recent sign-in"),
    "other_devices": _("other devices"),
    "oidc_group": _("the identity provider's group"),
}
PURPOSE_LABELS: dict[str, str] = {
    "oidc_link": _("linking an identity provider"),
    "proxy_link": _("linking a proxy sign-in"),
    "profile_link_oidc": _("linking an identity provider"),
    "profile_link_proxy": _("linking a proxy sign-in"),
    "profile_unlink": _("removing a sign-in method"),
    "logout_others": _("signing out other devices"),
    "admin_activate": _("activating an account"),
    "admin_deactivate": _("deactivating an account"),
    "admin_promote": _("granting the administrator role"),
    "admin_demote": _("removing the administrator role"),
    "admin_create_user": _("creating an account"),
}
FIELD_LABELS: dict[str, str] = {
    "name": _("name"),
    "base_currency": _("currency"),
    "country_code": _("country"),
    "timezone": _("time zone"),
    "locale": _("language"),
    "kind": _("kind"),
    "treatment": _("treatment"),
    "color": _("colour"),
    "date": _("date"),
    "amount": _("amount"),
    "description": _("description"),
    "category": _("category"),
}
KIND_LABELS: dict[str, str] = {
    "expense": _("expense"),
    "income": _("income"),
}
TREATMENT_LABELS: dict[str, str] = {
    "regular": _("regular"),
    "savings": _("savings"),
    "transfer": _("transfer"),
}
VALUE_TABLES: dict[str, dict[str, str]] = {
    "role": ROLE_LABELS,
    "from": ROLE_LABELS,
    "to": ROLE_LABELS,
    "reason": REASON_LABELS,
    "method": METHOD_LABELS,
    "via": METHOD_LABELS,
    "confirmed": METHOD_LABELS,
    "source": METHOD_LABELS,
    "scope": METHOD_LABELS,
    "purpose": PURPOSE_LABELS,
    "fields": FIELD_LABELS,
    "kind": KIND_LABELS,
    "direction": KIND_LABELS,
    "treatment": TREATMENT_LABELS,
    "previous_treatment": TREATMENT_LABELS,
}


def value_label(key: str, code: str) -> str | None:
    """The message id for a code under ``key`` (None: show the code itself)."""
    return VALUE_TABLES.get(key, {}).get(code)


# Filters: action groups per scope (explicit action lists, so a group never grows by prefix).
GROUPS: dict[Scope, dict[str, tuple[str, ...]]] = {
    "workspace": {
        # Setup records its event in the workspace it creates.
        "members": ("setup.create_admin", "member.role_changed", "member.removed"),
        "invitations": (
            "invitation.created",
            "invitation.revoked",
            "invitation.accepted",
            "invitation.email_mismatch",
        ),
        "settings": ("workspace.settings_updated",),
        "categories": (
            "category.created",
            "category.updated",
            "category.archived",
            "category.restored",
            "category.deleted",
            "category.seeded",
        ),
        "months": ("month.closed", "month.reopened"),
        "transactions": (
            "transaction.created",
            "transaction.updated",
            "transaction.deleted",
            "transaction.restored",
        ),
    },
    "instance": {
        "sign_in": ("auth.login_success", "auth.logout", "auth.register"),
        "failures": (
            "auth.login_failure",
            "auth.oidc_failure",
            "auth.proxy_failure",
            "auth.password_confirm_failure",
        ),
        "links": (
            "auth.oidc_linked",
            "auth.proxy_linked",
            "auth.oidc_unlinked",
            "auth.proxy_unlinked",
        ),
        "accounts": (
            "cli.create_admin",
            "cli.reset_password",
            "auth.password_change",
            "auth.instance_admin_granted",
            "auth.instance_admin_revoked",
            *AUDIT_DATA_KEYS,
        ),
    },
}
GROUP_LABELS: dict[str, str] = {
    "members": _("Members"),
    "invitations": _("Invitations"),
    "settings": _("Workspace settings"),
    "categories": _("Categories"),
    "months": _("Closing months"),
    "transactions": _("Transactions"),
    "sign_in": _("Sign-ins"),
    "failures": _("Failures"),
    "links": _("Linked sign-in methods"),
    "accounts": _("Account administration"),
}


class BadAuditRequest(ValueError):
    """A malformed cursor, date or filter value (the page answers 400)."""


_CURSOR = re.compile(r"(\d{1,20})\.([0-9a-f]{32})")


def encode_cursor(event: AuditEvent) -> str:
    micros = int(event.ts.timestamp() * 1_000_000)
    return f"{micros}.{event.id.hex}"


def decode_cursor(value: str) -> AuditCursor:
    match = _CURSOR.fullmatch(value)
    if match is None:
        raise BadAuditRequest("cursor")
    try:
        ts = datetime.fromtimestamp(int(match.group(1)) / 1_000_000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise BadAuditRequest("cursor") from exc
    return AuditCursor(ts=ts, id=UUID(hex=match.group(2)))


def parse_day(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BadAuditRequest("date") from exc


def parse_account(value: str) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError as exc:
        raise BadAuditRequest("account") from exc


@dataclass(frozen=True, slots=True)
class AuditQuery:
    scope: Scope
    group: str | None = None
    since: date | None = None  # first day included (UTC)
    until: date | None = None  # last day included (UTC)
    account: UUID | None = None  # instance scope: the affected account
    cursor: AuditCursor | None = None
    limit: int = PAGE_SIZE


@dataclass(frozen=True, slots=True)
class AuditRow:
    ts: datetime
    action: str
    label: str  # untranslated message id
    actor_name: str | None
    actor_email: str | None  # instance scope only
    actor_kind: str  # "user", "cli", "system"
    target_name: str | None  # the affected account (workspace scope: a member)
    target_email: str | None  # instance scope only
    target_missing: bool  # an affected user that no longer resolves ("Former member")
    ip_prefix: str | None  # instance scope only
    details: tuple[tuple[str, str | int | bool], ...]  # (key, value), allowlisted


@dataclass(frozen=True, slots=True)
class AuditPage:
    rows: tuple[AuditRow, ...]
    next_cursor: str | None


def _label(event: AuditEvent) -> str:
    if event.action == "auth.oidc_failure" and event.data.get("via") == "profile":
        return LINK_FAILURE
    return ACTION_LABELS.get(event.action, UNKNOWN_ACTION)


def _details(event: AuditEvent) -> tuple[tuple[str, str | int | bool], ...]:
    keys = DATA_KEYS.get(event.action, ())
    out: list[tuple[str, str | int | bool]] = []
    for key in keys:
        value = event.data.get(key)
        if isinstance(value, (str, int, bool)) and not isinstance(value, float):
            out.append((key, value))
    return tuple(out)


def _filter(query: AuditQuery) -> AuditFilter:
    actions: tuple[str, ...] | None = None
    if query.group is not None:
        groups = GROUPS[query.scope]
        if query.group not in groups:
            raise BadAuditRequest("group")
        actions = groups[query.group]
    since = datetime.combine(query.since, time(), tzinfo=UTC) if query.since else None
    # The day after the last day; date.max has none, so it leaves the range open-ended.
    until = (
        datetime.combine(query.until + timedelta(days=1), time(), tzinfo=UTC)
        if query.until is not None and query.until < date.max
        else None
    )
    return AuditFilter(actions=actions, since=since, until=until, before=query.cursor)


def _users(session: Session, ids: set[UUID]) -> dict[UUID, User]:
    if not ids:
        return {}
    return {user.id: user for user in session.scalars(select(User).where(User.id.in_(ids)))}


def _as_uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _affected_user(event: AuditEvent, *, instance: bool) -> UUID | None:
    """The account an event is about, if any.

    Instance events are about ``entity_id`` when it is a user. Workspace member events point at
    the membership row (gone after a removal); the member is ``data["user_id"]``.
    """
    if not instance:
        member = event.data.get("user_id")
        if isinstance(member, str):
            return _as_uuid(member)
    return _as_uuid(event.entity_id) if event.entity_type == "user" else None


def _page(session: Session, query: AuditQuery, events: list[AuditEvent], limit: int) -> AuditPage:
    more = len(events) > limit
    events = events[:limit]
    instance = query.scope == "instance"
    actor_ids = {event.actor_user_id for event in events if event.actor_user_id}
    target_ids = {
        uid for event in events if (uid := _affected_user(event, instance=instance)) is not None
    }
    people = _users(session, actor_ids | target_ids)
    rows: list[AuditRow] = []
    for event in events:
        actor = people.get(event.actor_user_id) if event.actor_user_id else None
        target_id = _affected_user(event, instance=instance)
        target = people.get(target_id) if target_id is not None else None
        rows.append(
            AuditRow(
                ts=event.ts,
                action=event.action,
                label=_label(event),
                actor_name=actor.display_name if actor else None,
                actor_email=actor.email if actor and instance else None,
                actor_kind=event.actor_type,
                target_name=target.display_name if target else None,
                target_email=target.email if target and instance else None,
                target_missing=target_id is not None and target is None,
                ip_prefix=event.ip_prefix if instance else None,
                details=_details(event),
            )
        )
    next_cursor = encode_cursor(events[-1]) if more and events else None
    return AuditPage(tuple(rows), next_cursor)


def workspace_page(factory: sessionmaker[Session], ctx: Context, query: AuditQuery) -> AuditPage:
    """The current workspace's events, for its owner."""
    require_role(ctx, Role.OWNER)
    limit = max(1, min(query.limit, MAX_PAGE_SIZE))
    with factory() as session:
        events = AuditEventRepository(session, ctx).page(_filter(query), limit=limit + 1)
        return _page(session, query, events, limit)


def instance_page(
    factory: sessionmaker[Session], *, actor_id: UUID, query: AuditQuery
) -> AuditPage:
    """Instance-level events (``workspace_id IS NULL``), for an instance administrator."""
    limit = max(1, min(query.limit, MAX_PAGE_SIZE))
    with factory() as session:
        require_instance_admin(session, actor_id)
        entity_id = str(query.account) if query.account is not None else None
        events = InstanceAuditReader(session).page(
            _filter(query), limit=limit + 1, entity_id=entity_id
        )
        return _page(session, query, events, limit)
