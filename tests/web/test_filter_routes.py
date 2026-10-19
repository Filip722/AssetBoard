"""Filtering the transaction tables by a category of the month summary (F2.4 part B), over
HTTP: the filtered page, the summary links, writes that stay filtered, and ids that are not
filters. The tests clear the seeded categories and build their own ("Golden ...")."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.ledger import Category, Transaction
from tests.conftest import OwnerApp

NOW = "2026-10-14T00:30:00+00:00"
MONTH = "/m/2026-10"


@pytest.fixture
def ledger(owner: OwnerApp) -> OwnerApp:
    owner.app.state.clock = FixedClock(NOW)  # today: October 2026
    owner.clear_categories()
    return owner


def _category(app: OwnerApp, name: str, kind: str = "expense", treatment: str = "regular") -> UUID:
    form = {"name": name, "kind": kind, "treatment": treatment, "color": "#16a34a"}
    assert app.post("/categories", form, token_from="/categories").status_code == 303
    with app.db() as session:
        return session.scalars(
            select(Category.id).where(Category.name == name, Category.kind == kind)
        ).one()


def _form(category: UUID, amount: str = "1", description: str = "", **extra: str) -> dict:
    return {
        "direction": "expense",
        "day": "7",
        "amount": amount,
        "description": description,
        "category_id": str(category),
    } | extra


def _add(app: OwnerApp, category: UUID, description: str, direction: str = "expense") -> None:
    form = _form(category, description=description, direction=direction)
    assert app.post(f"{MONTH}/transactions", form, token_from=MONTH).status_code == 303


def _htmx(app: OwnerApp, path: str, data: dict[str, str] | None = None, *, page: str = MONTH):
    headers = {"HX-Request": "true", "X-CSRF-Token": app.token(page)}
    return app.client.post(path, data=data or {}, headers=headers, follow_redirects=False)


def _id(app: OwnerApp, description: str) -> UUID:
    with app.db() as session:
        return session.scalars(
            select(Transaction.id).where(Transaction.description == description)
        ).one()


def _ledger(html: str) -> str:
    match = re.search(r'<div id="ledger".*?</section>\s*</div>', html, re.DOTALL)
    assert match is not None
    return match.group(0)


def _descriptions(html: str) -> list[str]:
    return re.findall(r"data-description>([^<]*)<", html)


@pytest.fixture
def golden(ledger: OwnerApp) -> dict[str, UUID]:
    ids = {
        "food": _category(ledger, "Golden Food"),
        "fuel": _category(ledger, "Golden Fuel"),
        "salary": _category(ledger, "Golden Salary", "income"),
    }
    _add(ledger, ids["food"], "Bread")
    _add(ledger, ids["food"], "Milk")
    _add(ledger, ids["fuel"], "Diesel")
    _add(ledger, ids["salary"], "Pay", "income")
    return ids


def test_a_category_filter_shows_only_its_table_and_entries(
    ledger: OwnerApp, golden: dict[str, UUID]
) -> None:
    food = golden["food"]
    page = ledger.client.get(f"{MONTH}?category={food}").text
    tables = _ledger(page)
    assert f'data-ledger-filter="{food}"' in tables
    assert 'id="ledger-expense"' in tables and 'id="ledger-income"' not in tables  # Q3
    assert sorted(_descriptions(tables)) == ["Bread", "Milk"]
    assert "Showing Golden Food only (2 entries)." in tables
    assert re.search(
        r'href="/m/2026-10#ledger" hx-get="/m/2026-10"[^>]*data-ledger-show-all', tables
    )
    # The entry row preselects the category, keeps it when cleared, and every form carries it.
    entry = re.search(r'<tr id="entry-expense-row".*?</tr>', tables, re.DOTALL)
    assert entry is not None
    assert f'<option value="{food}" selected>' in entry.group(0)
    assert f'data-default="{food}"' in entry.group(0)
    assert (
        tables.count(f'<input type="hidden" name="filter" value="{food}">') == 3
    )  # entry + 2 deletes
    assert f"?edit={_id(ledger, 'Bread')}&amp;category={food}#tx-" in tables
    # The summary marks the category; every category name links to its filter.
    link = re.search(rf'<a href="/m/2026-10\?category={food}#ledger"[^>]*>', page)
    assert link is not None
    assert 'aria-current="true"' in link.group(0)
    assert f'hx-get="/m/2026-10?category={food}"' in link.group(0)
    assert 'hx-select="#ledger" hx-target="#ledger"' in link.group(0)
    assert 'hx-push-url="true"' in link.group(0)
    other = re.search(rf'<a href="/m/2026-10\?category={golden["fuel"]}#ledger"[^>]*>', page)
    assert other is not None and "aria-current" not in other.group(0)
    ledger.client.headers["Accept-Language"] = "pl"
    polish = ledger.client.get(f"{MONTH}?category={food}").text
    assert "Pokazano tylko: Golden Food (2 wpisy)." in polish and ">Pokaż wszystko</a>" in polish
    assert 'role="status" data-ledger-filter-bar' not in page  # announced via #ledger-status
    assert 'data-ledger-announce="Showing Golden Food only (2 entries)."' in page


def test_an_income_category_shows_the_income_table(
    ledger: OwnerApp, golden: dict[str, UUID]
) -> None:
    tables = _ledger(ledger.client.get(f"{MONTH}?category={golden['salary']}").text)
    assert 'id="ledger-income"' in tables and 'id="ledger-expense"' not in tables
    assert _descriptions(tables) == ["Pay"]
    assert "Showing Golden Salary only (1 entry)." in tables


@pytest.mark.parametrize("value", ["not-a-uuid", str(uuid4()), ""])
def test_ids_that_are_no_category_of_this_workspace_show_everything(
    ledger: OwnerApp, golden: dict[str, UUID], value: str
) -> None:
    page = ledger.client.get(f"{MONTH}?category={value}")
    assert page.status_code == 200
    tables = _ledger(page.text)
    assert 'id="ledger-expense"' in tables and 'id="ledger-income"' in tables
    assert "data-ledger-filter-bar" not in tables and 'data-ledger-filter=""' in tables


def test_another_workspaces_category_is_no_filter(
    ledger: OwnerApp, golden: dict[str, UUID]
) -> None:
    other = ledger.add_workspace("Other")
    with ledger.db() as session, session.begin():
        foreign = Category(
            workspace_id=other, name="Golden Food", name_key="golden food", kind="expense",
            treatment="regular", color="#16a34a", sort_order=0,
        )  # fmt: skip
        session.add(foreign)
        session.flush()
        foreign_id = foreign.id
    tables = _ledger(ledger.client.get(f"{MONTH}?category={foreign_id}").text)
    assert "data-ledger-filter-bar" not in tables
    assert sorted(_descriptions(tables)) == ["Bread", "Diesel", "Milk", "Pay"]


def test_htmx_gets_the_page_to_select_the_tables_from(
    ledger: OwnerApp, golden: dict[str, UUID]
) -> None:
    food = golden["food"]
    response = ledger.client.get(f"{MONTH}?category={food}", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert sorted(_descriptions(_ledger(response.text))) == ["Bread", "Milk"]


def test_writes_stay_filtered_without_javascript(ledger: OwnerApp, golden: dict[str, UUID]) -> None:
    food, fuel = golden["food"], golden["fuel"]
    page = f"{MONTH}?category={food}"
    response = ledger.post(
        f"{MONTH}/transactions", _form(food, description="Eggs", filter=str(food)), token_from=page
    )
    location = response.headers["location"]
    assert response.status_code == 303 and f"category={food}" in location
    assert "notice=transaction_added" in location
    # An entry saved under another category is saved, and the page says it is not shown.
    response = ledger.post(
        f"{MONTH}/transactions", _form(fuel, description="Petrol", filter=str(food)),
        token_from=page,
    )  # fmt: skip
    location = response.headers["location"]
    assert "notice=outside_filter" in location and f"category={food}" in location
    shown = ledger.client.get(location.split("#")[0]).text
    assert "Its category is not shown while this filter is on." in shown
    assert "Petrol" not in _descriptions(_ledger(shown))
    bread = _id(ledger, "Bread")
    response = ledger.post(f"{MONTH}/transactions/{bread}/delete", {"filter": str(food)},
                           token_from=page)  # fmt: skip
    assert f"category={food}" in response.headers["location"]


def test_htmx_writes_answer_with_the_filtered_table(
    ledger: OwnerApp, golden: dict[str, UUID]
) -> None:
    food, fuel = golden["food"], golden["fuel"]
    page = f"{MONTH}?category={food}"
    html = _htmx(ledger, f"{MONTH}/transactions", _form(fuel, description="Petrol",
                 filter=str(food)), page=page).text  # fmt: skip
    section = re.search(r'<section id="ledger-expense".*?</section>', html, re.DOTALL)
    assert section is not None
    assert sorted(_descriptions(section.group(0))) == ["Bread", "Milk"]
    assert "Its category is not shown while this filter is on." in section.group(0)
    assert f'<option value="{food}" selected>' in section.group(0)  # still preselected
    link = re.search(rf'<a href="/m/2026-10\?category={food}#ledger"[^>]*>', html)
    assert link is not None and 'aria-current="true"' in link.group(0)  # the summary, OOB
    milk = _id(ledger, "Milk")
    edit = {"day": "8", "amount": "2", "description": "Milk", "category_id": str(food),
            "filter": str(food)}  # fmt: skip
    html = _htmx(ledger, f"{MONTH}/transactions/{milk}", edit, page=page).text
    assert 'id="ledger-income"' not in html and "Diesel" not in html
    deleted = _htmx(ledger, f"{MONTH}/transactions/{milk}/delete", {"filter": str(food)}, page=page)
    assert f'hx-post="/m/2026-10/transactions/{milk}/undo?filter={food}"' in deleted.text
    undo = _htmx(ledger, f"{MONTH}/transactions/{milk}/undo?filter={food}", page=page)
    assert sorted(_descriptions(undo.text)) == ["Bread", "Milk"]


def test_archived_and_transfer_categories_can_be_filtered(ledger: OwnerApp) -> None:
    old = _category(ledger, "Golden Old")
    moving = _category(ledger, "Golden Move", treatment="transfer")
    _add(ledger, old, "Lamp")
    _add(ledger, moving, "To savings")
    assert ledger.post(f"/categories/{old}/archive", {}, token_from="/categories").status_code
    tables = _ledger(ledger.client.get(f"{MONTH}?category={old}").text)
    assert _descriptions(tables) == ["Lamp"]
    assert f'<option value="{old}"' not in tables  # archived: no new entries, no preselection
    tables = _ledger(ledger.client.get(f"{MONTH}?category={moving}").text)  # Q4
    assert _descriptions(tables) == ["To savings"]
    assert f'<option value="{moving}" selected>' in tables


def test_viewers_and_closed_months_can_filter_read_only(
    ledger: OwnerApp, golden: dict[str, UUID]
) -> None:
    food = golden["food"]
    assert ledger.post(f"{MONTH}/close", token_from=MONTH).status_code == 303
    tables = _ledger(ledger.client.get(f"{MONTH}?category={food}").text)
    assert sorted(_descriptions(tables)) == ["Bread", "Milk"]
    assert "data-entry-row" not in tables and 'name="filter"' not in tables
    ledger.set_admin_role("viewer")
    tables = _ledger(ledger.client.get(f"{MONTH}?category={food}").text)
    assert "Showing Golden Food only (2 entries)." in tables and "data-entry-row" not in tables


def _htmx_get(app: OwnerApp, path: str) -> Any:
    return app.client.get(path, headers={"HX-Request": "true"}, follow_redirects=False)


def test_opening_and_cancelling_an_edit_keep_the_filter(
    ledger: OwnerApp, golden: dict[str, UUID]
) -> None:
    food, milk = golden["food"], _id(ledger, "Milk")
    edit = _htmx_get(ledger, f"{MONTH}/transactions/{milk}/edit?filter={food}")
    assert edit.status_code == 200
    assert f'<input type="hidden" name="filter" value="{food}">' in edit.text
    assert f"/m/2026-10?category={food}#tx-{milk}" in edit.text  # Cancel without JavaScript
    row = _htmx_get(ledger, f"{MONTH}/transactions/{milk}/row?filter={food}")
    assert f'<input type="hidden" name="filter" value="{food}">' in row.text  # its delete form
    # A row outside the filter (an old page, another tab) is shown unfiltered, never an error.
    diesel = _id(ledger, "Diesel")
    other = _htmx_get(ledger, f"{MONTH}/transactions/{diesel}/edit?filter={food}")
    assert other.status_code == 200 and 'name="filter"' not in other.text
    plain = ledger.client.get(f"{MONTH}/transactions/{milk}/edit?filter={food}",
                              follow_redirects=False)  # fmt: skip
    assert plain.headers["location"] == f"/m/2026-10?edit={milk}&category={food}#tx-{milk}"


def test_an_htmx_edit_answers_with_the_filter_bar_and_field(
    ledger: OwnerApp, golden: dict[str, UUID]
) -> None:
    food, milk = golden["food"], _id(ledger, "Milk")
    edit = {"day": "8", "amount": "3", "description": "Milk", "category_id": str(food),
            "filter": str(food)}  # fmt: skip
    html = _htmx(ledger, f"{MONTH}/transactions/{milk}", edit, page=f"{MONTH}?category={food}").text
    assert "Showing Golden Food only (2 entries)." in html
    assert f'<input type="hidden" name="filter" value="{food}">' in html
    assert sorted(_descriptions(html)) == ["Bread", "Milk"]


def test_a_no_javascript_edit_keeps_the_filter_and_says_when_it_hides_the_entry(
    ledger: OwnerApp, golden: dict[str, UUID]
) -> None:
    food, fuel, milk = golden["food"], golden["fuel"], _id(ledger, "Milk")
    page = f"{MONTH}?category={food}"
    edit = {"day": "8", "amount": "3", "description": "Milk", "category_id": str(food),
            "filter": str(food)}  # fmt: skip
    response = ledger.post(f"{MONTH}/transactions/{milk}", edit, token_from=page)
    assert response.headers["location"] == (
        f"/m/2026-10?notice=transaction_saved&category={food}#tx-{milk}"
    )
    moved = edit | {"category_id": str(fuel)}
    response = ledger.post(f"{MONTH}/transactions/{milk}", moved, token_from=page)
    assert response.headers["location"] == (
        f"/m/2026-10?notice=outside_filter&category={food}#ledger-expense"
    )


def test_a_no_javascript_undo_keeps_the_filter(ledger: OwnerApp, golden: dict[str, UUID]) -> None:
    food, bread = golden["food"], _id(ledger, "Bread")
    page = f"{MONTH}?category={food}"
    deleted = ledger.post(f"{MONTH}/transactions/{bread}/delete", {"filter": str(food)},
                          token_from=page)  # fmt: skip
    shown = ledger.client.get(deleted.headers["location"].split("#")[0]).text
    undo_form = re.search(r"<form[^>]*/undo.*?</form>", shown, re.DOTALL)
    assert undo_form is not None
    assert f'<input type="hidden" name="filter" value="{food}">' in undo_form.group(0)
    undo = ledger.post(f"{MONTH}/transactions/{bread}/undo", {"filter": str(food)},
                       token_from=page)  # fmt: skip
    assert undo.headers["location"] == (
        f"/m/2026-10?notice=transaction_restored&category={food}#tx-{bread}"
    )


@pytest.mark.parametrize("value", ["unknown", "foreign", "injected"])
def test_a_filter_that_is_no_category_here_is_dropped_from_answers(
    ledger: OwnerApp, golden: dict[str, UUID], value: str
) -> None:
    raw = {
        "unknown": str(uuid4()),
        "foreign": "",
        "injected": "x\r\nSet-Cookie: a=b&category=evil",
    }[value]
    if value == "foreign":
        other = ledger.add_workspace("Other")
        with ledger.db() as session, session.begin():
            foreign = Category(
                workspace_id=other, name="Golden Food", name_key="golden food", kind="expense",
                treatment="regular", color="#16a34a", sort_order=0,
            )  # fmt: skip
            session.add(foreign)
            session.flush()
            raw = str(foreign.id)
    form = _form(golden["fuel"], description="Petrol", filter=raw)
    response = ledger.post(f"{MONTH}/transactions", form, token_from=MONTH)
    location = response.headers["location"]
    assert "category=" not in location and "outside_filter" not in location
    assert "evil" not in location and "Set-Cookie" not in location
    html = _htmx(ledger, f"{MONTH}/transactions", _form(golden["fuel"], filter=raw)).text
    assert "not shown while this filter is on" not in html
    assert 'id="ledger-expense"' in html and "data-ledger-filter-bar" not in html


def test_an_income_filter_does_not_filter_an_expense_write(
    ledger: OwnerApp, golden: dict[str, UUID]
) -> None:
    form = _form(golden["fuel"], description="Petrol", filter=str(golden["salary"]))
    response = ledger.post(f"{MONTH}/transactions", form, token_from=MONTH)
    assert "category=" not in response.headers["location"]
    assert "outside_filter" not in response.headers["location"]
