from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from assetboard.web.context import CurrentContext
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

# Every router carries the CSRF dependency, so a new state-changing route is covered by default.
router = APIRouter(dependencies=[Depends(require_csrf)])


@router.get("/", name="home")
async def home(request: Request, ctx: CurrentContext) -> object:
    return render_template(request, "home.html", context={"workspace": ctx.workspace})
