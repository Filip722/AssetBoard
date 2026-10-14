"""Month pages over HTTP (F2.5): navigation, picker, closing, roles and CSP-safe markup."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.identity import AuditEvent, User
from assetboard.services.months import MonthClosed
from tests.conftest import OwnerApp

NO_PERMISSION = "You do not have permission to view this page."


@pytest.fixture
def months(owner: OwnerApp) -> OwnerApp:
    # Today: October 2026. Only past instants: the access gate checks sessions with this clock.
    owner.app.state.clock = FixedClock("2026-10-14T00:30:00+00:00")
    return owner


def _page(app: OwnerApp, path: str) -> str:
    response = app.client.get(path)
    assert response.status_code == 200, response.text
    return response.text


def _href(html: str, marker: str) -> str | None:
    match = re.search(rf'<a href="([^"]+)"[^>]*{marker}', html)
    return match.group(1) if match else None


def _close(app: OwnerApp, month: str) -> object:
    return app.post(f"/m/{month}/close", token_from=f"/m/{month}")


# Navigation


def test_m_redirects_to_the_current_month(months: OwnerApp) -> None:
    response = months.client.get("/m", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/m/2026-10")


def test_the_current_month_is_judged_in_the_workspace_time_zone(months: OwnerApp) -> None:
    # Past instants only: the access gate checks session lifetimes against the same clock.
    months.app.state.clock = FixedClock("2026-09-30T23:30:00+00:00")
    assert months.client.get("/m", follow_redirects=False).headers["location"] == "/m/2026-09"
    token = months.token("/workspace/settings")
    form = {"name": "Home", "base_currency": "EUR", "country_code": "", "timezone": "Europe/Warsaw"}
    form |= {"locale": "en", "csrf_token": token}
    assert months.client.post("/workspace/settings", data=form).status_code == 200
    assert months.client.get("/m", follow_redirects=False).headers["location"] == "/m/2026-10"


def test_a_month_page_links_its_neighbours_and_today(months: OwnerApp) -> None:
    page = _page(months, "/m/2026-09")
    assert "<h1" in page and "September 2026" in page
    assert _href(page, "data-month-prev") == "/m/2026-08"
    assert _href(page, "data-month-next") == "/m/2026-10"
    assert _href(page, "data-month-today") == "/m/2026-10"
    assert 'data-month-today aria-current="date"' not in page
    assert 'data-month-today aria-current="date"' in _page(months, "/m/2026-10")


@pytest.mark.parametrize(
    ("month", "prev", "next_"),
    [("2026-12", "/m/2026-11", "/m/2027-01"), ("2027-01", "/m/2026-12", "/m/2027-02")],
)
def test_neighbours_cross_the_year(months: OwnerApp, month: str, prev: str, next_: str) -> None:
    page = _page(months, f"/m/{month}")
    assert (_href(page, "data-month-prev"), _href(page, "data-month-next")) == (prev, next_)


@pytest.mark.parametrize(
    ("month", "missing"), [("1900-01", "data-month-prev"), ("2199-12", "data-month-next")]
)
def test_the_range_ends_have_no_link_beyond(months: OwnerApp, month: str, missing: str) -> None:
    page = _page(months, f"/m/{month}")
    assert missing not in page and 'aria-disabled="true"' in page


@pytest.mark.parametrize(
    "path",
    [
        "/m/2026-13",
        "/m/2026-00",
        "/m/2026-1",
        "/m/26-10",
        "/m/abc",
        "/m/2026-10-01",
        "/m/1899-12",
        "/m/2200-01",
        "/m/%EF%BC%92%EF%BC%90%EF%BC%92%EF%BC%96-10",  # full-width digits
    ],
)
def test_invalid_or_out_of_range_months_are_not_found(months: OwnerApp, path: str) -> None:
    response = months.client.get(path)
    assert response.status_code == 404
    assert "The page you requested could not be found." in response.text


def test_empty_and_future_months_render(months: OwnerApp) -> None:
    for path in ("/m/2026-10", "/m/2030-05", "/m/1950-02"):
        page = _page(months, path)
        assert "Nothing in this month yet" in page


def test_the_month_name_follows_the_language(months: OwnerApp) -> None:
    months.client.headers["Accept-Language"] = "pl"
    page = _page(months, "/m/2026-10")
    # Capitalised only as a heading and title; CLDR's lowercase form in running text.
    heading = re.search(r"<h1[^>]*>\s*([^<]+?)\s*<", page)
    assert heading is not None and heading.group(1) == "Październik 2026"
    assert "<title>Październik 2026 · " in page and "Dzisiaj" in page
    assert 'aria-label="Następny miesiąc: listopad 2026"' in page
    assert 'aria-label="Poprzedni miesiąc: wrzesień 2026"' in page
    assert 'data-confirm="Zamknąć miesiąc październik 2026? ' in page
    assert 'placeholder="RRRR-MM"' in page
    _close(months, "2026-10")
    assert 'data-confirm="Otworzyć ponownie miesiąc październik 2026? ' in _page(
        months, "/m/2026-10"
    )
    months.client.headers["Accept-Language"] = "en"
    assert 'placeholder="YYYY-MM"' in _page(months, "/m/2026-09")


def test_the_nav_month_link_needs_a_workspace_page(months: OwnerApp) -> None:
    """Like "Categories": pages without a workspace Context (account pages) do not show it."""
    account = _page(months, "/account")
    assert "data-month-link" not in account and "data-categories-link" not in account
    assert "data-month-link" in _page(months, "/")


def test_the_nav_month_link_points_to_the_current_month(months: OwnerApp) -> None:
    home = _page(months, "/")
    assert 'href="/m" data-month-link>' in home  # not current on the overview
    assert 'href="/m" data-month-link aria-current="page"' in _page(months, "/m/2026-10")
    assert 'data-month-link aria-current="page"' not in _page(months, "/categories")


# The month picker (no JavaScript needed)


def test_the_picker_is_a_plain_get_form_with_a_native_month_input(months: OwnerApp) -> None:
    page = _page(months, "/m/2026-09")
    form = re.search(
        r"<form method=\"get\" action=\"/m\"[^>]*data-month-picker>.*?</form>", page, re.S
    )
    assert form is not None
    assert 'type="month"' in form.group(0) and 'value="2026-09"' in form.group(0)
    assert 'name="from" value="2026-09"' in form.group(0) and 'type="submit"' in form.group(0)


@pytest.mark.parametrize(
    ("value", "location"),
    [("2026-03", "/m/2026-03"), ("03.2026", "/m/2026-03"), ("3/2026", "/m/2026-03")],
)
def test_the_picker_goes_to_the_chosen_month(months: OwnerApp, value: str, location: str) -> None:
    response = months.client.get(f"/m?month={value}&from=2026-09", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, location)


@pytest.mark.parametrize(
    ("query", "location"),
    [
        ("month=nonsense&from=2026-09", "/m/2026-09?error=invalid_month"),
        ("month=&from=2026-09", "/m/2026-09?error=invalid_month"),
        ("month=2200-01&from=2026-09", "/m/2026-09?error=invalid_month"),
        ("month=x&from=junk", "/m/2026-10?error=invalid_month"),
    ],
)
def test_an_invalid_picker_value_returns_with_an_error(
    months: OwnerApp, query: str, location: str
) -> None:
    response = months.client.get(f"/m?{query}", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, location)
    page = _page(months, location)
    assert "Enter a month as YYYY-MM, for example 2026-10." in page


# Closing and reopening


def test_an_owner_closes_and_reopens_a_month(months: OwnerApp) -> None:
    page = _page(months, "/m/2026-10")
    assert "data-month-close" in page and "data-closed-banner" not in page
    response = _close(months, "2026-10")
    assert (response.status_code, response.headers["location"]) == (  # type: ignore[attr-defined]
        303,
        "/m/2026-10?notice=closed",
    )
    page = _page(months, "/m/2026-10?notice=closed")
    assert "Month closed." in page and "data-closed-banner" in page and "data-closed-badge" in page
    assert "This month is closed. Its entries and planned values are read-only." in page
    assert "Closed on Oct 14, 2026 by Admin." in page
    assert "data-month-close" not in page and "data-month-reopen" in page

    response = months.post("/m/2026-10/reopen", token_from="/m/2026-10")
    assert response.headers["location"] == "/m/2026-10?notice=reopened"
    page = _page(months, "/m/2026-10?notice=reopened")
    assert "Month reopened." in page and "data-closed-banner" not in page
    with months.db() as session:
        actions = session.scalars(
            select(AuditEvent.action)
            .where(AuditEvent.action.startswith("month."))
            .order_by(AuditEvent.ts)
        ).all()
    assert actions == ["month.closed", "month.reopened"]


def test_a_no_op_close_or_reopen_says_so(months: OwnerApp) -> None:
    response = months.post("/m/2026-10/reopen", token_from="/m/2026-10")  # never closed
    assert response.headers["location"] == "/m/2026-10?notice=already_open"
    page = _page(months, "/m/2026-10?notice=already_open")
    assert "This month is already open." in page and "Month reopened." not in page
    _close(months, "2026-10")
    response = _close(months, "2026-10")  # a second tab, a double click
    assert response.headers["location"] == "/m/2026-10?notice=already_closed"  # type: ignore[attr-defined]
    page = _page(months, "/m/2026-10?notice=already_closed")
    assert "This month was already closed." in page and "Month closed." not in page
    with months.db() as session:
        actions = session.scalars(
            select(AuditEvent.action).where(AuditEvent.action.startswith("month."))
        ).all()
    assert actions == ["month.closed"]


def test_closed_on_is_the_date_in_the_workspace_time_zone(months: OwnerApp) -> None:
    token = months.token("/workspace/settings")
    form = {"name": "Home", "base_currency": "EUR", "country_code": "", "timezone": "Europe/Warsaw"}
    assert (
        months.client.post(
            "/workspace/settings", data=form | {"locale": "en", "csrf_token": token}
        ).status_code
        == 200
    )
    # 23:30 UTC on 30 September is 01:30 on 1 October in Warsaw (a past instant: see the fixture).
    months.app.state.clock = FixedClock("2026-09-30T23:30:00+00:00")
    assert _close(months, "2026-09").status_code == 303  # type: ignore[attr-defined]
    page = _page(months, "/m/2026-09")
    assert "Closed on Oct 1, 2026 by Admin." in page and "Sep 30, 2026" not in page


def test_the_closers_name_is_escaped_in_the_banner(months: OwnerApp) -> None:
    with months.db() as session, session.begin():
        admin = session.get(User, months.admin_id)
        assert admin is not None
        admin.display_name = '<b onmouseover="x()">Ann & Co</b>'
    _close(months, "2026-09")
    page = _page(months, "/m/2026-09")
    banner = re.search(r"data-closed-banner>.*?</section>", page, re.S)
    assert banner is not None
    assert "<b onmouseover" not in banner.group(0) and 'onmouseover="' not in banner.group(0)
    assert "&lt;b onmouseover=&#34;x()&#34;&gt;Ann &amp; Co&lt;/b&gt;" in banner.group(0)


def test_close_and_reopen_ask_for_confirmation(months: OwnerApp) -> None:
    page = _page(months, "/m/2026-10")
    assert (
        'data-confirm="Close October 2026? Its entries and planned values become read-only' in page
    )
    _close(months, "2026-10")
    page = _page(months, "/m/2026-10")
    assert 'data-confirm="Reopen October 2026? Its entries and planned values can be' in page


def test_editors_close_but_cannot_reopen(months: OwnerApp) -> None:
    months.set_admin_role("editor")
    assert "data-month-close" in _page(months, "/m/2026-10")
    assert _close(months, "2026-10").status_code == 303  # type: ignore[attr-defined]
    page = _page(months, "/m/2026-10")
    assert "data-closed-banner" in page and "data-month-reopen" not in page
    assert "Only an owner can reopen it." in page
    response = months.post("/m/2026-10/reopen", token_from="/m/2026-10")
    assert response.status_code == 403 and NO_PERMISSION in response.text
    assert "data-closed-banner" in _page(months, "/m/2026-10")


def test_viewers_see_the_closed_state_but_no_controls(months: OwnerApp) -> None:
    _close(months, "2026-09")
    months.set_admin_role("viewer")
    page = _page(months, "/m/2026-09")
    assert "data-closed-banner" in page and "Closed on Oct 14, 2026 by Admin." in page
    assert "data-month-reopen" not in page and "data-month-close" not in page
    assert "data-month-close" not in _page(months, "/m/2026-10")
    for action in ("close", "reopen"):
        response = months.post(f"/m/2026-10/{action}", token_from="/m/2026-10")
        assert response.status_code == 403 and NO_PERMISSION in response.text


def test_future_months_offer_no_close_and_refuse_it(months: OwnerApp) -> None:
    page = _page(months, "/m/2026-11")
    assert "data-month-close" not in page and "Future months stay open for planning." in page
    response = _close(months, "2026-11")
    assert response.headers["location"] == "/m/2026-11?error=month_in_future"  # type: ignore[attr-defined]
    page = _page(months, "/m/2026-11?error=month_in_future")
    assert "Future months cannot be closed; they stay open for planning." in page
    assert "data-closed-banner" not in page


def test_state_changes_need_the_csrf_token(months: OwnerApp) -> None:
    response = months.client.post("/m/2026-10/close", follow_redirects=False)
    assert response.status_code == 403
    assert "data-closed-banner" not in _page(months, "/m/2026-10")


def test_invalid_months_cannot_be_closed(months: OwnerApp) -> None:
    assert months.post("/m/2026-13/close", token_from="/m/2026-10").status_code == 404


def test_a_refused_write_to_a_closed_month_is_a_409(
    months: OwnerApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The application-wide safety net for routes that do not handle MonthClosed themselves."""
    from assetboard.domain.months import YearMonth
    from assetboard.web import month_routes

    def refuse(*_args: object) -> None:
        raise MonthClosed(YearMonth(2026, 10))

    monkeypatch.setattr(month_routes, "reopen_month", refuse)
    response = months.post("/m/2026-10/reopen", token_from="/m/2026-10")
    assert response.status_code == 409
    assert "This month is closed. Its entries and planned values cannot change" in response.text
    months.client.headers["Accept-Language"] = "pl"
    response = months.post("/m/2026-10/reopen", token_from="/m/2026-10")
    assert response.status_code == 409 and "Miesiąc zamknięty" in response.text


def test_month_pages_have_no_inline_scripts_or_styles(months: OwnerApp) -> None:
    _close(months, "2026-10")
    page = _page(months, "/m/2026-10")
    assert "<script>" not in page and not re.search(r"<script(?![^>]*\bsrc=)", page)
    assert " style=" not in page and "<style" not in page
    assert not re.search(r"\s(x-|@|:)[a-z]", page)  # no Alpine expressions
