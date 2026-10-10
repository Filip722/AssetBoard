from __future__ import annotations

from fastapi import APIRouter, Request

from assetboard.web.rendering import render_template

router = APIRouter()


@router.get("/", name="home")
async def home(request: Request) -> object:
    return render_template(request, "home.html")
