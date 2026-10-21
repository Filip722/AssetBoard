from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import pyotp
import segno
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.branding import APP_NAME
from assetboard.db.models.identity import User
from assetboard.i18n import gettext
from assetboard.services import totp
from assetboard.services.auth import MAX_PASSWORD_LENGTH, Throttled, WebSession, user_has_password
from assetboard.services.profile import ConfirmMethod
from assetboard.web.confirm import confirm_view, refusal, run_confirmed
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf, web_session
from assetboard.web.throttle import retry_after, throttled_message

router = APIRouter(dependencies=[Depends(require_csrf)])
FormStr = Annotated[str, Form()]


def _current(request: Request) -> WebSession:
    current = web_session(request)
    if current is None:
        raise HTTPException(status_code=404)
    return current


async def _page(
    request: Request,
    *,
    codes: list[str] | None = None,
    error: str | None = None,
    status: int = 200,
) -> Response:
    current = _current(request)
    app = request.app.state
    factor = await run_in_threadpool(
        lambda: totp.state(
            app.session_factory,
            user_id=current.user_id,
            session_hash=current.session_hash,
            settings=app.settings,
            clock=app.clock,
        )
    )
    qr = None
    if factor.secret is not None:
        uri = pyotp.TOTP(factor.secret).provisioning_uri(current.email, issuer_name=APP_NAME)
        qr = segno.make_qr(uri).svg_inline(scale=4, light="white")
    has_password = await run_in_threadpool(user_has_password, app.session_factory, current.user_id)
    return render_template(
        request,
        "account/totp.html",
        context={
            "factor": factor,
            "qr": qr,
            "codes": codes,
            "error": error,
            "has_password": has_password,
            "confirm": await confirm_view(request, current),
        },
        status_code=status,
    )


@router.get("/account/totp", name="account_totp")
async def page(request: Request) -> Response:
    return await _page(request)


@router.post("/account/totp/start", name="totp_start")
async def start(request: Request, confirm_password: FormStr = "") -> Response:
    current = _current(request)
    app = request.app.state

    def action(session: Session, user: User, method: ConfirmMethod) -> bool:
        return method == "password" and totp.begin_enrollment(
            session, user, session_hash=current.session_hash, settings=app.settings, clock=app.clock
        )

    return await _confirmed(request, current, confirm_password, "totp_enable", action)


@router.post("/account/totp/disable", name="totp_disable")
async def disable(request: Request, confirm_password: FormStr = "") -> Response:
    current = _current(request)

    def action(session: Session, user: User, method: ConfirmMethod) -> bool:
        return method == "password" and totp.disable(
            session, user, session_hash=current.session_hash, clock=request.app.state.clock
        )

    return await _confirmed(request, current, confirm_password, "totp_disable", action)


@router.post("/account/totp/recovery-codes", name="totp_recovery_codes")
async def recovery_codes(
    request: Request,
    confirm_password: FormStr = "",
    second_factor: FormStr = "",
) -> Response:
    current = _current(request)

    def action(session: Session, user: User, method: ConfirmMethod) -> bool | list[str]:
        if method != "password":
            return False
        codes = totp.regenerate_recovery_codes(
            session, user, session_hash=current.session_hash, clock=request.app.state.clock
        )
        return codes if codes is not None else False

    return await _confirmed(
        request,
        current,
        confirm_password,
        "totp_recovery_codes",
        action,
        second_factor=second_factor,
        show_codes=True,
    )


async def _confirmed(
    request: Request,
    current: WebSession,
    password: str,
    purpose: str,
    action: Callable[[Session, User, ConfirmMethod], bool | list[str]],
    *,
    second_factor: str | None = None,
    show_codes: bool = False,
) -> Response:
    if len(password) > MAX_PASSWORD_LENGTH:
        return await _page(
            request, error=gettext("The password is not correct.", request.state.locale), status=401
        )
    outcome = await run_confirmed(
        request,
        current,
        purpose=purpose,
        password=password,
        second_factor=second_factor,
        action=action,
    )
    if outcome.status != "confirmed":
        failed = refusal(request, outcome)
        response = await _page(request, error=failed.message, status=failed.status_code)
        if failed.retry_after:
            response.headers["Retry-After"] = failed.retry_after
        return response
    if show_codes:
        if not isinstance(outcome.value, list) or not all(
            isinstance(code, str) for code in outcome.value
        ):
            raise HTTPException(status_code=403)
        return await _page(request, codes=outcome.value)
    if not outcome.value:
        raise HTTPException(status_code=403)
    return RedirectResponse(request.url_for("account_totp").path, status_code=303)


@router.post("/account/totp/verify", name="totp_verify")
async def verify(request: Request, code: FormStr = "") -> Response:
    current = _current(request)
    app = request.app.state
    result = await run_in_threadpool(
        lambda: totp.complete_enrollment(
            app.session_factory,
            user_id=current.user_id,
            session_hash=current.session_hash,
            code=code,
            settings=app.settings,
            clock=app.clock,
        )
    )
    if isinstance(result, list):
        return await _page(request, codes=result)
    if isinstance(result, Throttled):
        response = await _page(
            request,
            error=throttled_message(result.until, app.clock.now(), request.state.locale),
            status=401,
        )
        response.headers["Retry-After"] = retry_after(result.until, app.clock.now()) or "1"
        return response
    return await _page(
        request,
        error=gettext("The code is not valid or enrollment has expired.", request.state.locale),
        status=400,
    )
