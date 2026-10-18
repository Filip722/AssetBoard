"""Planned values on the month page over HTTP (F2.3 part B): the plan in the summary block,
inline cells, the copy banner, the automatic copy on opening, closed months, roles, HTMX
fragments and plain forms."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.identity import Workspace
from assetboard.db.models.ledger import BudgetLine, Category
from tests.conftest import OwnerApp

NOW = "2026-10-14T00:30:00+00:00"
MONTH = "/m/2026-10"
SEPTEMBER = "/m/2026-09"


@pytest.fixture
def plan(owner: OwnerApp) -> OwnerApp:
    owner.app.state.clock = FixedClock(NOW)  # today: October 2026
    return owner


def _category(app: OwnerApp, name: str, kind: str = "expense", treatment: str = "regular") -> UUID:
    form = {"name": name, "kind": kind, "treatment": treatment, "color": "#16a34a"}
    assert app.post("/categories", form, token_from="/categories").status_code == 303
    with app.db() as session:
        return session.scalars(
            select(Category.id).where(Category.name == name, Category.kind == kind)
        ).one()


def _htmx(app: OwnerApp, path: str, data: dict[str, str] | None = None, *, month: str = MONTH):
    headers = {"HX-Request": "true", "X-CSRF-Token": app.token(month)}
    return app.client.post(path, data=data or {}, headers=headers, follow_redirects=False)


def _set(app: OwnerApp, category: UUID, amount: str, month: str = MONTH) -> Any:
    return app.post(f"{month}/plan/{category}", {"amount": amount}, token_from=month)


def _planned(app: OwnerApp, month: str = "2026-10") -> dict[UUID, int]:
    with app.db() as session:
        lines = session.scalars(select(BudgetLine)).all()
        return {
            line.category_id: line.planned_minor
            for line in lines
            if line.month.strftime("%Y-%m") == month
        }


def _page(app: OwnerApp, path: str = MONTH) -> str:
    response = app.client.get(path)
    assert response.status_code == 200, response.text
    return response.text


def _summary(html: str) -> str:
    match = re.search(r'<section id="month-summary".*?</section>', html, re.DOTALL)
    assert match is not None
    return match.group(0)


def _row(html: str, category: UUID) -> str:
    match = re.search(rf'<tr id="plan-{category}".*?</tr>', html, re.DOTALL)
    assert match is not None
    return match.group(0)


def _auto(app: OwnerApp, on: bool = True) -> None:
    with app.db() as session, session.begin():
        workspace = session.scalars(select(Workspace)).one()
        workspace.auto_copy_plan = on


# The plan in the summary block


def test_the_summary_lists_categories_in_groups_with_editable_cells(plan: OwnerApp) -> None:
    food = _category(plan, "Food")
    _category(plan, "Savings", treatment="savings")
    _category(plan, "Salary", "income")
    _category(plan, "Withdrawal", "income", "savings")
    moving = _category(plan, "Between accounts", treatment="transfer")
    summary = _summary(_page(plan))
    groups = re.findall(r'data-plan-group="([a-z]+)"', summary)
    assert groups == ["expense", "saved", "income", "withdrawn"]  # no transfer group
    for heading in ("Expenses", "Saved / invested", "Income", "Withdrawn from savings"):
        assert f">{heading}</th>" in summary
    row = _row(summary, food)
    assert f'action="{MONTH}/plan/{food}"' in row and 'hx-trigger="change, submit"' in row
    assert '<label for="plan-' in row and "Planned for Food" in row
    assert 'name="amount"' in row and 'inputmode="decimal"' in row and "data-plan-input" in row
    assert 'name="csrf_token"' in row
    assert f"plan-{moving}" not in summary
    assert "0 or an empty field removes it" in summary


def test_a_planned_value_is_saved_changed_and_removed_without_javascript(plan: OwnerApp) -> None:
    food = _category(plan, "Food")
    response = _set(plan, food, "550")
    assert response.status_code == 303
    assert response.headers["location"] == f"{MONTH}?notice=plan_saved#plan-{food}"
    assert _planned(plan) == {food: 55000}
    page = _page(plan, f"{MONTH}?notice=plan_saved")
    assert "Planned value saved." in page
    assert re.search(r'id="plan-[^"]*-amount"[^>]*value="550.00"', _row(page, food))
    assert _set(plan, food, "600.5").status_code == 303 and _planned(plan) == {food: 60050}
    response = _set(plan, food, "")
    assert response.headers["location"].endswith(f"notice=plan_removed#plan-{food}")
    assert _planned(plan) == {}


def test_htmx_saves_answer_with_the_row_status_and_banner(plan: OwnerApp) -> None:
    food = _category(plan, "Food")
    response = _htmx(plan, f"{MONTH}/plan/{food}", {"amount": "1,234.5"})
    assert response.status_code == 200 and "HX-Request" in response.headers["Vary"]
    html = response.text
    assert html.lstrip().startswith(f'<tr id="plan-{food}"')
    assert 'value="1234.50"' in html and 'data-saved-value="1234.50"' in html
    assert '<p id="plan-status" hx-swap-oob="innerHTML">Planned value saved.</p>' in html
    assert '<div id="plan-banner" hx-swap-oob="outerHTML">' in html
    removed = _htmx(plan, f"{MONTH}/plan/{food}", {"amount": "0"}).text
    assert "Planned value removed." in removed and 'value=""' in _row(removed, food)


def test_a_refused_value_keeps_what_was_typed_with_the_message(plan: OwnerApp) -> None:
    food = _category(plan, "Food")
    _set(plan, food, "10")
    htmx = _htmx(plan, f"{MONTH}/plan/{food}", {"amount": "-5"})
    assert htmx.status_code == 400
    row = _row(htmx.text, food)
    assert 'value="-5"' in row and 'data-saved-value="10.00"' in row
    assert 'aria-invalid="true"' in row and "A planned value cannot be negative." in row
    assert "data-autofocus" not in row  # the user may be typing in the next cell
    page = _set(plan, food, "12.345")
    assert page.status_code == 400
    row = _row(page.text, food)
    assert "Too many decimal places for EUR." in row and "data-autofocus" in row
    plan.client.headers["Accept-Language"] = "pl"
    polish = _htmx(plan, f"{MONTH}/plan/{food}", {"amount": "abc"})
    assert "Podaj prawidłową kwotę." in polish.text
    assert _planned(plan) == {food: 1000}


def test_archived_and_transfer_lines_stay_editable_but_take_no_new_value(plan: OwnerApp) -> None:
    food, rent = _category(plan, "Food"), _category(plan, "Rent")
    _set(plan, food, "10")
    form = {"name": "Rent", "kind": "expense", "treatment": "transfer", "color": "#16a34a"}
    assert plan.post(f"/categories/{rent}", form, token_from="/categories").status_code == 303
    # The category left the plan (a stale page): reload with the message, HTMX or not.
    refused = _htmx(plan, f"{MONTH}/plan/{rent}", {"amount": "5"})
    assert refused.status_code == 409
    assert refused.headers["HX-Redirect"] == f"{MONTH}?error=plan_category_transfer"
    plain = _set(plan, rent, "5")
    assert plain.headers["location"] == f"{MONTH}?error=plan_category_transfer"
    page = _page(plan, plain.headers["location"])
    assert re.search(r"data-error>Transfers are not counted in the plan", page)
    assert _planned(plan) == {food: 1000}
    assert plan.post(f"/categories/{food}/archive", {}, token_from="/categories").status_code == 303
    summary = _summary(_page(plan))
    assert "archived" in _row(summary, food)
    removed = _htmx(plan, f"{MONTH}/plan/{food}", {"amount": ""})
    assert removed.status_code == 200 and f'<tr id="plan-{food}"' not in removed.text
    archived = _htmx(plan, f"{MONTH}/plan/{food}", {"amount": "3"})  # no line any more
    assert archived.headers["HX-Redirect"] == f"{MONTH}?error=plan_category_archived"
    page = _page(plan, f"{MONTH}?error=plan_category_archived")
    assert "This category is archived: it takes no new planned values." in page
    assert "transfer" not in re.findall(r'data-plan-group="([a-z]+)"', _summary(_page(plan)))


def test_an_unknown_category_reloads_the_page(plan: OwnerApp) -> None:
    response = _set(plan, UUID(int=1), "5")
    assert response.headers["location"] == f"{MONTH}?error=plan_category_gone"
    assert _set(plan, "not-a-uuid", "5").status_code == 404  # type: ignore[arg-type]
    htmx = _htmx(plan, f"{MONTH}/plan/{UUID(int=1)}", {"amount": "5"})
    assert htmx.status_code == 404
    assert htmx.headers["HX-Redirect"] == f"{MONTH}?error=plan_category_gone"
    assert "This category no longer exists." in _page(plan, f"{MONTH}?error=plan_category_gone")


def test_writes_need_a_csrf_token(plan: OwnerApp) -> None:
    food = _category(plan, "Food")
    response = plan.client.post(f"{MONTH}/plan/{food}", data={"amount": "5"})
    assert response.status_code == 403 and _planned(plan) == {}


# The copy banner


def test_an_empty_month_offers_to_copy_the_previous_plan(plan: OwnerApp) -> None:
    food, salary = _category(plan, "Food"), _category(plan, "Salary", "income")
    assert "data-plan-banner" not in _page(plan)  # September has no plan
    _set(plan, food, "550", SEPTEMBER)
    _set(plan, salary, "3000", SEPTEMBER)
    summary = _summary(_page(plan))
    assert "This month has no plan yet." in summary
    assert "Copy plan from September 2026" in summary
    assert f'hx-post="{MONTH}/plan/copy"' in summary and 'hx-target="#month-summary"' in summary
    plan.client.headers["Accept-Language"] = "pl"
    assert "Kopiuj plan z września 2026" in _page(plan)
    del plan.client.headers["Accept-Language"]
    response = plan.post(f"{MONTH}/plan/copy", token_from=MONTH)
    assert response.headers["location"] == (f"{MONTH}?plan_copied=2&plan_skipped=0#month-summary")
    assert _planned(plan) == {food: 55000, salary: 300000}
    page = _page(plan, response.headers["location"].split("#")[0])
    assert "Copied 2 planned values from September 2026." in page
    assert "data-plan-banner" not in page  # the month has a plan now


def test_copy_never_overwrites_and_reports_skipped_lines(plan: OwnerApp) -> None:
    food, old = _category(plan, "Food"), _category(plan, "Old")
    _set(plan, food, "550", SEPTEMBER)
    _set(plan, old, "5", SEPTEMBER)
    assert plan.post(f"/categories/{old}/archive", {}, token_from="/categories").status_code == 303
    htmx = _htmx(plan, f"{MONTH}/plan/copy")
    assert htmx.status_code == 200
    # The result is announced on the status line, which sits outside the block.
    assert (
        '<p id="plan-status" hx-swap-oob="innerHTML">Copied 1 planned value from September 2026.'
        " 1 planned value of an archived or transfer category was not copied.</p>"
    ) in htmx.text
    summary = _summary(htmx.text)
    assert "Copied 1 planned value from September 2026." in summary
    assert "1 planned value of an archived or transfer category was not copied." in summary
    assert summary.count("data-autofocus") == 1  # the first planned cell
    _set(plan, food, "700")
    again = _htmx(plan, f"{MONTH}/plan/copy")
    assert "Nothing was copied" in again.text
    assert _planned(plan) == {food: 70000}


def test_the_banner_is_hidden_from_viewers_and_in_closed_months(plan: OwnerApp) -> None:
    food = _category(plan, "Food")
    _set(plan, food, "550", SEPTEMBER)
    assert plan.post(f"{MONTH}/close", token_from=MONTH).status_code == 303
    page = _page(plan)
    assert "data-plan-banner" not in page and "data-plan-input" not in page
    response = plan.post(f"{MONTH}/plan/copy", token_from=SEPTEMBER)
    assert response.headers["location"] == f"{MONTH}?error=month_closed"
    response = plan.post(f"{MONTH}/plan/{food}", {"amount": "1"}, token_from=SEPTEMBER)
    assert response.headers["location"] == f"{MONTH}?error=month_closed"
    htmx = _htmx(plan, f"{MONTH}/plan/{food}", {"amount": "1"}, month=SEPTEMBER)
    assert htmx.status_code == 409 and htmx.headers["HX-Redirect"] == f"{MONTH}?error=month_closed"
    assert _planned(plan) == {}


def test_viewers_see_planned_values_read_only_and_cannot_write(plan: OwnerApp) -> None:
    food, rent = _category(plan, "Food"), _category(plan, "Rent")
    _set(plan, food, "1234.5")
    _set(plan, rent, "1", SEPTEMBER)
    plan.set_admin_role("viewer")
    summary = _summary(_page(plan))
    assert "data-plan-input" not in summary and "data-plan-banner" not in summary
    assert re.search(r"data-planned>€1,234.50<", _row(summary, food))
    assert '<span class="sr-only">Not planned</span>' in _row(summary, rent)
    assert "aria-label" not in _row(summary, rent)
    assert "Copied" not in _page(plan, f"{MONTH}?plan_copied=2")  # not for viewers
    response = plan.post(f"{MONTH}/plan/{food}", {"amount": "1"}, token_from=MONTH)
    assert response.status_code == 403
    assert plan.post(f"{MONTH}/plan/copy", token_from=MONTH).status_code == 403


# The automatic copy when a month is opened


def test_opening_a_month_copies_the_plan_once_when_the_setting_is_on(plan: OwnerApp) -> None:
    food = _category(plan, "Food")
    _set(plan, food, "550", SEPTEMBER)
    assert "data-plan-banner" in _page(plan) and _planned(plan) == {}  # off by default
    _auto(plan)
    assert plan.client.head(MONTH).status_code == 405 and _planned(plan) == {}  # no HEAD
    page = _page(plan)
    assert "1 planned value was copied from September 2026 (workspace setting)." in page
    assert _planned(plan) == {food: 55000}
    _set(plan, food, "")
    page = _page(plan)  # emptied on purpose: not copied again; the banner offers it
    assert _planned(plan) == {} and "data-plan-banner" in page
    assert "workspace setting" not in page


def test_the_automatic_copy_leaves_past_months_and_viewers_alone(plan: OwnerApp) -> None:
    food = _category(plan, "Food")
    _set(plan, food, "1", "/m/2026-08")
    _auto(plan)
    assert "data-plan-banner" in _page(plan, SEPTEMBER)  # a past month: the banner only
    assert _planned(plan, "2026-09") == {}
    _set(plan, food, "2", SEPTEMBER)
    plan.set_admin_role("viewer")
    _page(plan)
    assert _planned(plan) == {}
    plan.set_admin_role("owner")
    _page(plan)
    assert _planned(plan) == {food: 200}


# Transaction writes refresh the block


def test_a_transaction_write_sends_the_plan_out_of_band(plan: OwnerApp) -> None:
    food = _category(plan, "Food")
    _set(plan, food, "550")
    form = {
        "direction": "expense",
        "day": "7",
        "amount": "12.50",
        "description": "",
        "category_id": str(food),
    }
    html = _htmx(plan, f"{MONTH}/transactions", form).text
    summary = _summary(html)
    assert 'hx-swap-oob="outerHTML"' in summary.split(">")[0]
    assert 'value="550.00"' in _row(summary, food)


@pytest.mark.parametrize("value", ["²", "9" * 5000, "-1", "1.5", ""])
def test_a_malformed_copy_report_is_ignored(plan: OwnerApp, value: str) -> None:
    page = _page(plan, f"{MONTH}?plan_copied={value}&plan_skipped=0")
    assert "data-plan-copied" not in page
    page = _page(plan, f"{MONTH}?plan_copied=1&plan_skipped={value or 'x'}")
    assert "data-plan-copied" not in page


def test_the_status_line_stays_outside_the_summary(plan: OwnerApp) -> None:
    _category(plan, "Food")
    page = _page(plan)
    assert 'id="plan-status"' in page and 'id="plan-status"' not in _summary(page)
