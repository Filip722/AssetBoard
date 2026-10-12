"""The per-request ``Context`` dependency for routes that work on workspace data."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from starlette.concurrency import run_in_threadpool

from assetboard.services.context import Context, build_context
from assetboard.web.security import web_session


async def current_context(request: Request) -> Context:
    """Build the Context from the signed-in session (the access gate guarantees one).

    Raises ``NoWorkspace`` (rendered as a 403 page) when the user has no membership.
    """
    signed_in = web_session(request)
    if signed_in is None:
        raise RuntimeError("current_context needs a signed-in request")
    state = request.app.state

    def build() -> Context:
        with state.session_factory() as session:
            return build_context(session, signed_in, clock=state.clock)

    ctx = await run_in_threadpool(build)
    request.state.context = ctx
    return ctx


CurrentContext = Annotated[Context, Depends(current_context)]
