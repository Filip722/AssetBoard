"""The audit log page (F1.5 part C): ``GET /audit?scope=workspace|instance``.

Filters (query parameters):

* ``group``: an action group of the scope (workspace: members, invitations, settings;
  instance: sign_in, failures, links, accounts);
* ``from`` / ``to``: a date range in UTC, both days included (``YYYY-MM-DD``);
* ``account``: instance scope only, the affected account's id (the Users page links to it);
* ``cursor``: the keyset position from the previous page's "Older" link;
* ``limit``: page size, at most ``MAX_PAGE_SIZE``.

A malformed cursor, date, group, account or limit answers 400. A bare ``/audit`` opens the
workspace log for the owner of the current workspace, else the instance log for an instance
administrator, else 403. Who may see which scope is decided in
``services.audit_view`` (owner of the current workspace; instance administrator).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from assetboard.i18n import gettext
from assetboard.services.admin_users import NotInstanceAdmin
from assetboard.services.audit_view import (
    GROUP_LABELS,
    GROUPS,
    KEY_LABELS,
    MAX_PAGE_SIZE,
    PAGE_SIZE,
    AuditPage,
    AuditQuery,
    AuditRow,
    BadAuditRequest,
    Scope,
    decode_cursor,
    instance_page,
    parse_account,
    parse_day,
    value_label,
    workspace_page,
)
from assetboard.services.context import NoWorkspace, Role
from assetboard.web.context import current_context
from assetboard.web.errors import error_response
from assetboard.web.rendering import render_template
from assetboard.web.security import web_session

router = APIRouter()


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


ACTOR_KINDS = {"cli": _("Command line"), "system": _("System")}
FORMER_MEMBER = _("Former member")


def _t(request: Request, message: str) -> str:
    return gettext(message, request.state.locale)


def _value(request: Request, key: str, value: str | int | bool) -> str:
    if isinstance(value, bool):
        return _t(request, _("yes") if value else _("no"))
    if isinstance(value, int):
        return str(value)
    if "," in value:  # a list of codes, such as changed workspace fields
        return ", ".join(_value(request, key, part) for part in value.split(",") if part)
    label = value_label(key, value)
    return _t(request, label) if label else value


def _row(request: Request, row: AuditRow) -> dict[str, Any]:
    actor = row.actor_name
    if actor is None:
        kind = ACTOR_KINDS.get(row.actor_kind)
        actor = _t(request, kind) if kind else None
    target = row.target_name
    if target is None and row.target_missing:
        target = _t(request, FORMER_MEMBER)
    return {
        "when": row.ts.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "label": _t(request, row.label),
        "actor": actor,
        "actor_email": row.actor_email,
        "target": target,
        "target_email": row.target_email,
        "ip_prefix": row.ip_prefix,
        "details": [
            (_t(request, KEY_LABELS[key]), _value(request, key, value))
            for key, value in row.details
        ],
    }


def _bad_request(request: Request) -> Response:
    return error_response(
        request,
        400,
        _t(request, _("Invalid request")),
        _t(
            request,
            _("The filters or the page position are not valid. Start again from the first page."),
        ),
    )


async def _owns_current_workspace(request: Request) -> bool:
    try:
        ctx = await current_context(request)
    except NoWorkspace:
        return False
    return ctx.role == Role.OWNER


@router.get("/audit", name="audit")
async def audit(request: Request) -> Response:
    current = web_session(request)
    if current is None:  # the gate redirects first
        return error_response(request, 404, _t(request, _("Page not found")), "")
    params = request.query_params
    scope_value = params.get("scope")
    if scope_value is None:
        # A bare /audit: the owner's workspace log, else the administrator's instance log.
        if await _owns_current_workspace(request):
            scope_value = "workspace"
        elif current.is_instance_admin:
            scope_value = "instance"
        else:
            raise NotInstanceAdmin
    if scope_value not in GROUPS:
        return _bad_request(request)
    scope: Scope = "instance" if scope_value == "instance" else "workspace"
    try:
        limit = int(params.get("limit", PAGE_SIZE))
    except ValueError:
        return _bad_request(request)
    state = request.app.state
    try:
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise BadAuditRequest("limit")
        cursor_value = params.get("cursor", "")
        query = AuditQuery(
            scope=scope,
            group=params.get("group") or None,
            since=parse_day(params.get("from", "")),
            until=parse_day(params.get("to", "")),
            account=parse_account(params.get("account", "")) if scope == "instance" else None,
            cursor=decode_cursor(cursor_value) if cursor_value else None,
            limit=limit,
        )
        if scope == "instance":
            if not current.is_instance_admin:
                raise NotInstanceAdmin
            page: AuditPage = await run_in_threadpool(
                lambda: instance_page(state.session_factory, actor_id=current.user_id, query=query)
            )
        else:
            ctx = await current_context(request)
            page = await run_in_threadpool(
                lambda: workspace_page(state.session_factory, ctx, query)
            )
    except BadAuditRequest:
        return _bad_request(request)

    filters = {
        key: value
        for key in ("group", "from", "to", "account", "limit")
        if (value := params.get(key)) and (key != "account" or scope == "instance")
    }
    next_url = (
        "/audit?" + urlencode({"scope": scope, **filters, "cursor": page.next_cursor})
        if page.next_cursor
        else None
    )
    unfiltered_account = {key: value for key, value in filters.items() if key != "account"}
    return render_template(
        request,
        "audit/list.html",
        context={
            "scope": scope,
            "rows": [_row(request, row) for row in page.rows],
            "groups": [(key, _t(request, GROUP_LABELS[key])) for key in GROUPS[scope]],
            "filters": filters,
            "next_url": next_url,
            "first_url": "/audit?" + urlencode({"scope": scope, **filters}),
            "all_accounts_url": "/audit?" + urlencode({"scope": scope, **unfiltered_account}),
            "paged": bool(params.get("cursor")),
        },
    )
