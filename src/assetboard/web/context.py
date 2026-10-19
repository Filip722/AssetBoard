"""The per-request ``Context`` dependency for routes that work on workspace data."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from starlette.concurrency import run_in_threadpool

from assetboard.services.context import Context, WorkspaceNav, build_context, workspace_nav
from assetboard.services.instance_settings import workspace_creation_allowed
from assetboard.web.security import web_session


async def current_context(request: Request) -> Context:
    """Build the Context from the signed-in session (the access gate guarantees one).

    Raises ``NoWorkspace`` (rendered as a 403 page) when the user has no membership. Also sets
    ``request.state.workspace_nav`` for the page chrome (workspace name, switcher, owner links).
    Pages without a Context (account, error and signed-out pages) have no ``workspace_nav``
    and ``base.html`` renders without the workspace parts.
    """
    signed_in = web_session(request)
    if signed_in is None:
        raise RuntimeError("current_context needs a signed-in request")
    state = request.app.state

    def build() -> tuple[Context, WorkspaceNav]:
        with state.session_factory() as session:
            ctx = build_context(session, signed_in, clock=state.clock)
            can_create = workspace_creation_allowed(
                session, is_instance_admin=signed_in.is_instance_admin
            )
            return ctx, workspace_nav(session, ctx, can_create_workspace=can_create)

    ctx, nav = await run_in_threadpool(build)
    request.state.context = ctx
    request.state.workspace_nav = nav
    return ctx


CurrentContext = Annotated[Context, Depends(current_context)]
