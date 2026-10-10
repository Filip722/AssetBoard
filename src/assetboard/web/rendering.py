from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse

from assetboard.domain.money import Money
from assetboard.i18n import translations_for
from assetboard.i18n.numbers import format_money

_TEMPLATE_DIR = Path(__file__).parent / "templates"


@lru_cache(maxsize=2)
def templates_for(locale: str) -> Jinja2Templates:
    templates = Jinja2Templates(directory=_TEMPLATE_DIR)
    templates.env.add_extension("jinja2.ext.i18n")
    translations = translations_for(locale)
    cast(Any, templates.env).install_gettext_translations(translations, newstyle=True)
    templates.env.globals.update(Money=Money, format_money=format_money)
    templates.env.globals.update(static_url=static_url, scoped_url=scoped_url, uuid4=uuid4)
    return templates


def scoped_url(request: Request, route_name: str, **path_params: str) -> str:
    route_path = request.app.url_path_for(route_name, **path_params)
    root_path = request.scope.get("root_path", "").rstrip("/")
    return f"{root_path}{route_path}"


def static_url(request: Request, path: str) -> str:
    return f"{scoped_url(request, 'static', path=path)}?v={request.app.version}"


def render_template(
    request: Request,
    name: str,
    *,
    context: dict[str, object] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    locale = request.state.locale
    settings = request.app.state.settings
    template_context: dict[str, object] = {
        "locale": locale,
        "app_name": request.app.state.app_name,
        "default_currency": settings.default_currency,
    }
    if context:
        template_context.update(context)
    return templates_for(locale).TemplateResponse(
        request=request,
        name=name,
        context=template_context,
        status_code=status_code,
    )
