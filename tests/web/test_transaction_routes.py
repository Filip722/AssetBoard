"""Transactions on the month page over HTTP (F2.2 part B): the two tables, entry and edit rows,
delete and undo, autocomplete, closed months, roles, HTMX fragments and plain forms."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import pytest
from babel.messages.pofile import read_po
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.ledger import Category, Transaction
from assetboard.i18n import gettext
from assetboard.web.ledger import FIELD_ERRORS, KIND_MISMATCH
from tests.conftest import OwnerApp

NOW = "2026-10-14T00:30:00+00:00"
NO_PERMISSION = "You do not have permission to view this page."
MONTH = "/m/2026-10"
PART_A_CODES = {
    "invalid_date",
    "day_not_in_month",
    "date_out_of_range",
    "invalid_amount",
    "amount_not_positive",
    "amount_too_large",
    "too_many_decimals",
    "invalid_description",
    "description_too_long",
    "category_required",
    "invalid_category",
    "category_kind_mismatch",
    "category_archived",
    "invalid_direction",
}


@pytest.fixture
def ledger(owner: OwnerApp) -> OwnerApp:
    # Today: October 2026. Only past instants: the access gate checks sessions with this clock.
    owner.app.state.clock = FixedClock(NOW)
    return owner


def _category(app: OwnerApp, name: str, kind: str = "expense") -> UUID:
    form = {"name": name, "kind": kind, "treatment": "regular", "color": "#16a34a"}
    response = app.post("/categories", form, token_from="/categories")
    assert response.status_code == 303, response.text
    with app.db() as session:
        return session.scalars(
            select(Category.id).where(Category.name == name, Category.kind == kind)
        ).one()


def _form(category: UUID | str, day: str = "7", amount: str = "12.50", **values: str) -> dict:
    return {
        "direction": "expense",
        "day": day,
        "amount": amount,
        "description": "Groceries",
        "category_id": str(category),
    } | values


def _add(app: OwnerApp, data: dict[str, str], month: str = MONTH) -> Any:
    return app.post(f"{month}/transactions", data, token_from=month)


def _htmx(app: OwnerApp, path: str, data: dict[str, str] | None = None, *, month: str = MONTH):
    headers = {"HX-Request": "true", "X-CSRF-Token": app.token(month)}
    return app.client.post(path, data=data or {}, headers=headers, follow_redirects=False)


def _htmx_get(app: OwnerApp, path: str) -> Any:
    return app.client.get(path, headers={"HX-Request": "true"}, follow_redirects=False)


def _rows(app: OwnerApp) -> list[Transaction]:
    with app.db() as session:
        return list(session.scalars(select(Transaction).order_by(Transaction.created_at)))


def _id(app: OwnerApp, description: str) -> UUID:
    return next(row.id for row in _rows(app) if row.description == description)


def _section(html: str, direction: str) -> str:
    match = re.search(rf'<section id="ledger-{direction}".*?</section>', html, re.DOTALL)
    assert match is not None
    return match.group(0)


def _descriptions(html: str, direction: str) -> list[str]:
    return re.findall(r"data-description>([^<]*)<", _section(html, direction))


def _page(app: OwnerApp, path: str = MONTH) -> str:
    response = app.client.get(path)
    assert response.status_code == 200, response.text
    return response.text


# The page


def test_the_month_page_has_two_tables_with_entry_rows(ledger: OwnerApp) -> None:
    _category(ledger, "Food")
    _category(ledger, "Salary", "income")
    page = _page(ledger)
    for direction in ("expense", "income"):
        section = _section(page, direction)
        assert f'<form id="entry-{direction}"' in section
        for name in ("day", "amount", "description", "category_id"):
            assert re.search(rf'name="{name}" form="entry-{direction}"', section)
        assert f'<label for="entry-{direction}-day" class="sr-only">Day</label>' in section
        assert f'value="{direction}"' in section
        assert f'aria-describedby="ledger-{direction}-hint"' in section
        assert "Enter saves, Esc clears the row or cancels an edit." in section
    assert "<h2" in page and ">Expenses</h2>" in page and ">Income</h2>" in page
    assert 'id="month-summary"' in page and 'id="ledger-status"' in page
    expense = _section(page, "expense")
    assert ">Food</option>" in expense and ">Salary</option>" not in expense
    assert ">Salary</option>" in _section(page, "income")


def test_rows_are_ordered_by_date_then_entry_and_amounts_follow_the_locale(
    ledger: OwnerApp,
) -> None:
    food = _category(ledger, "Food")
    for day, description in (("9", "Late"), ("3", "First"), ("9", "Later still")):
        assert _add(ledger, _form(food, day, "1234.5", description=description)).status_code == 303
    page = _page(ledger)
    assert _descriptions(page, "expense") == ["First", "Late", "Later still"]
    assert "€1,234.50" in page
    assert '<time datetime="2026-10-03"><span aria-hidden="true">3</span>' in page
    assert '<span class="sr-only">Oct 3, 2026</span>' in page
    ledger.client.headers["Accept-Language"] = "pl"
    assert "1234,50 €" in _page(ledger).replace(" ", " ").replace("1 234", "1234")


def test_a_category_less_table_links_the_categories_page(ledger: OwnerApp) -> None:
    _category(ledger, "Food")
    page = _page(ledger)
    income = _section(page, "income")
    assert "Add an income category first." in income and 'href="/categories"' in income
    assert "entry-income" not in income


# Adding


def test_a_day_number_is_saved_in_the_viewed_month(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    response = _add(ledger, _form(food, "31", "12,5"), "/m/2026-12")
    assert response.headers["location"] == (
        "/m/2026-12?notice=transaction_added&entry=expense&day=31#ledger-expense"
    )
    (row,) = _rows(ledger)
    assert (str(row.date), row.amount_minor, row.currency) == ("2026-12-31", 1250, "EUR")
    assert "Entry added." in _page(ledger, "/m/2026-12?notice=transaction_added")


def test_htmx_add_returns_the_section_with_a_new_entry_row_keeping_the_day(
    ledger: OwnerApp,
) -> None:
    food = _category(ledger, "Food")
    response = _htmx(ledger, f"{MONTH}/transactions", _form(food, "2026-10-07"))
    assert response.status_code == 200
    html = response.text
    assert html.lstrip().startswith('<section id="ledger-expense"')
    assert _descriptions(html, "expense") == ["Groceries"]
    entry = re.search(r'<tr id="entry-expense-row".*?</tr>', html, re.DOTALL)
    assert entry is not None
    assert 'value=""' in entry.group(0) and "data-autofocus" in entry.group(0)
    # The day is kept, as a day number of the viewed month; the rest is empty.
    assert re.search(r'id="entry-expense-day"[^>]*value="7"[^>]*data-autofocus', entry.group(0))
    assert re.search(r'id="entry-expense-amount"[^>]*value=""', entry.group(0))
    assert re.search(r'id="entry-expense-description"[^>]*value=""', entry.group(0))
    assert '<option value="" selected>' not in entry.group(0)  # no category chosen
    assert "selected>Food" not in entry.group(0)
    assert '<p id="ledger-status" hx-swap-oob="innerHTML">Entry added.</p>' in html
    assert '<div id="month-summary" data-month-summary hx-swap-oob="outerHTML"></div>' in html
    assert "HX-Request" in response.headers["Vary"]


def test_a_full_date_in_another_month_is_saved_there_and_linked(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    response = _add(ledger, _form(food, "07.09.2026"))
    location = f"{MONTH}?saved_in=2026-09&entry=expense&day=07.09.2026#ledger-expense"
    assert response.headers["location"] == location
    page = _page(ledger, f"{MONTH}?saved_in=2026-09")
    assert (
        '<a href="/m/2026-09" class="font-medium underline">Entry saved in September 2026.</a>'
        in page
    )
    assert _descriptions(page, "expense") == []
    html = _htmx(ledger, f"{MONTH}/transactions", _form(food, "2026-11-02")).text
    assert '<a href="/m/2026-11"' in html and "Entry saved in November 2026." in html
    assert [str(r.date) for r in _rows(ledger)] == ["2026-09-07", "2026-11-02"]


@pytest.mark.parametrize(
    ("day", "message"),
    [
        ("abc", "Enter a day of the month (1-31) or a date such as 2026-10-07 or 07.10.2026."),
        ("2026/10/07", "Enter a day of the month (1-31)"),
        ("31", "That month has no such day."),
        ("29.02.2027", "That month has no such day."),
        ("01.01.2300", "Enter a date between the years 1900 and 2199."),
    ],
)
def test_day_errors_are_shown_on_the_day_field(ledger: OwnerApp, day: str, message: str) -> None:
    food = _category(ledger, "Food")
    response = _add(ledger, _form(food, day), "/m/2026-11")
    assert response.status_code == 400
    section = _section(response.text, "expense")
    assert message in section
    assert 'aria-describedby="ledger-expense-hint entry-expense-day-error"' in section
    assert 'aria-invalid="true"' in section and f'value="{day}"' in section
    assert "The entry was not saved. Check the marked fields." in section
    assert _rows(ledger) == []


@pytest.mark.parametrize(
    ("amount", "locale", "minor"),
    [
        ("1 234,56", "pl", 123456),
        ("1,234.56", "en", 123456),
        ("12,5", "pl", 1250),
        ("7", "en", 700),
    ],
)
def test_amounts_are_read_in_the_locale_format(
    ledger: OwnerApp, amount: str, locale: str, minor: int
) -> None:
    food = _category(ledger, "Food")
    ledger.client.headers["Accept-Language"] = locale
    assert _add(ledger, _form(food, amount=amount)).status_code == 303
    assert [r.amount_minor for r in _rows(ledger)] == [minor]


@pytest.mark.parametrize(
    ("amount", "message"),
    [
        ("0", "The amount must be greater than zero."),
        ("-5", "The amount must be greater than zero."),
        ("12.345", "Too many decimal places for EUR."),
        ("10000000000000000", "The amount is too large."),
        ("twelve", "Enter a valid amount."),
        ("", "Enter a valid amount."),
    ],
)
def test_amount_errors(ledger: OwnerApp, amount: str, message: str) -> None:
    food = _category(ledger, "Food")
    response = _add(ledger, _form(food, amount=amount))
    assert response.status_code == 400 and message in response.text
    assert 'id="entry-expense-amount-error"' in response.text


def test_every_invalid_field_is_reported_at_once_and_the_first_is_focused(
    ledger: OwnerApp,
) -> None:
    _category(ledger, "Food")
    data = _form("", "x", "0", description="a" * 201)
    response = _htmx(ledger, f"{MONTH}/transactions", data)
    assert response.status_code == 400
    html = response.text
    for name in ("day", "amount", "description", "category"):
        assert f'id="entry-expense-{name}-error"' in html
    assert "Use at most 200 characters." in html and "Choose a category." in html
    assert re.search(r'id="entry-expense-day"[^>]*data-autofocus', html)
    assert html.count("data-autofocus") == 1


def test_description_and_category_errors(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    salary = _category(ledger, "Salary", "income")
    cases = [
        (_form(food, description="bad\u0000text"), "not allowed"),
        (_form(salary), "Choose an expense category."),
        (
            _form(salary, direction="income") | {"category_id": str(food)},
            "Choose an income category.",
        ),
        (_form("not-a-uuid"), "Choose one of the listed categories."),
        (_form(UUID(int=7)), "Choose one of the listed categories."),
    ]
    for data, message in cases:
        response = _add(ledger, data)
        assert response.status_code == 400 and message in response.text, message
    response = _add(ledger, _form(food, direction="sideways"))
    assert response.status_code == 400 and "Reload the page and try again." in response.text
    assert _rows(ledger) == []


def test_archived_categories_are_offered_only_on_rows_that_have_them(ledger: OwnerApp) -> None:
    old = _category(ledger, "Old")
    _category(ledger, "Food")
    _add(ledger, _form(old, description="Kept"))
    assert ledger.post(f"/categories/{old}/archive", token_from="/categories").status_code == 303
    page = _page(ledger)
    entry = re.search(r'<tr id="entry-expense-row".*?</tr>', page, re.DOTALL)
    assert entry is not None and ">Old" not in entry.group(0)
    row_id = _id(ledger, "Kept")
    edit = _htmx_get(ledger, f"{MONTH}/transactions/{row_id}/edit").text
    assert f'<option value="{old}" selected>Old (archived)</option>' in edit
    response = _add(ledger, _form(old))
    assert response.status_code == 400
    assert "This category is archived. Choose another one." in response.text


def test_every_field_error_code_has_a_polish_message() -> None:
    assert set(FIELD_ERRORS) == PART_A_CODES
    messages = [m for m in FIELD_ERRORS.values() if m] + list(KIND_MISMATCH.values())
    with open("src/assetboard/locales/pl/LC_MESSAGES/messages.po", encoding="utf-8") as po:
        catalog = read_po(po)
    for message in messages:
        entry = catalog.get(message)
        assert entry is not None and entry.string and not entry.fuzzy, message
        assert gettext(message, "pl") != message


def test_messages_follow_the_language(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    ledger.client.headers["Accept-Language"] = "pl"
    response = _add(ledger, _form(food, "31", "0"), "/m/2026-11")
    assert "Ten miesiąc nie ma takiego dnia." in response.text
    assert "Kwota musi być większa od zera." in response.text
    assert ">Wydatki</h2>" in response.text


# Editing


def test_edit_opens_an_edit_row_with_the_stored_values(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    _add(ledger, _form(food, "7", "1234.5"))
    row_id = _id(ledger, "Groceries")
    ledger.client.headers["Accept-Language"] = "pl"
    response = _htmx_get(ledger, f"{MONTH}/transactions/{row_id}/edit")
    html = response.text
    assert response.status_code == 200 and html.lstrip().startswith(f'<tr id="tx-{row_id}"')
    assert "data-edit-row" in html and f'<form id="edit-{row_id}"' in html
    assert 'value="7"' in html and 'value="1234,50"' in html and 'value="Groceries"' in html
    assert f'<option value="{food}" selected>' in html
    assert re.search(rf'id="edit-{row_id}-day"[^>]*data-autofocus', html)
    cancel = _htmx_get(ledger, f"{MONTH}/transactions/{row_id}/row").text
    assert "data-edit-row" not in cancel and re.search(r"data-autofocus data-edit", cancel)


def test_edit_without_javascript_uses_the_page(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    _add(ledger, _form(food))
    row_id = _id(ledger, "Groceries")
    response = ledger.client.get(f"{MONTH}/transactions/{row_id}/edit", follow_redirects=False)
    assert response.headers["location"] == f"{MONTH}?edit={row_id}#tx-{row_id}"
    page = _page(ledger, f"{MONTH}?edit={row_id}")
    assert "data-edit-row" in page and f'href="{MONTH}#tx-{row_id}"' in page
    assert (
        ledger.client.get(f"{MONTH}/transactions/{row_id}/row", follow_redirects=False).headers[
            "location"
        ]
        == MONTH
    )
    assert "data-edit-row" not in _page(ledger, f"{MONTH}?edit=nonsense")


def test_an_update_saves_and_returns_the_section(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    _add(ledger, _form(food))
    row_id = _id(ledger, "Groceries")
    data = {"day": "2", "amount": "3.10", "description": "Bread", "category_id": str(food)}
    response = _htmx(ledger, f"{MONTH}/transactions/{row_id}", data)
    assert response.status_code == 200
    assert _descriptions(response.text, "expense") == ["Bread"]
    assert "Entry saved." in response.text and 'id="month-summary"' in response.text
    assert re.search(r"data-autofocus data-edit", response.text)
    (row,) = _rows(ledger)
    assert (str(row.date), row.amount_minor) == ("2026-10-02", 310)
    plain = ledger.post(f"{MONTH}/transactions/{row_id}", data | {"day": "3"}, token_from=MONTH)
    assert plain.headers["location"] == f"{MONTH}?notice=transaction_saved#tx-{row_id}"


def test_a_refused_update_rerenders_the_edit_row(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    _add(ledger, _form(food))
    row_id = _id(ledger, "Groceries")
    data = {"day": "40", "amount": "3.10", "description": "Bread", "category_id": str(food)}
    response = _htmx(ledger, f"{MONTH}/transactions/{row_id}", data)
    assert response.status_code == 400
    assert "data-edit-row" in response.text and 'value="40"' in response.text
    assert f'id="edit-{row_id}-day-error"' in response.text
    page = ledger.post(f"{MONTH}/transactions/{row_id}", data, token_from=MONTH)
    assert page.status_code == 400 and "data-edit-row" in page.text and "<html" in page.text


def test_moving_an_entry_to_another_month_links_it(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    _add(ledger, _form(food))
    row_id = _id(ledger, "Groceries")
    data = {"day": "2026-09-30", "amount": "1", "description": "Moved", "category_id": str(food)}
    response = _htmx(ledger, f"{MONTH}/transactions/{row_id}", data)
    assert "Entry saved in September 2026." in response.text
    assert _descriptions(response.text, "expense") == []


# Deleting and undo


def test_delete_returns_the_section_and_an_undo_toast(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    _add(ledger, _form(food))
    row_id = _id(ledger, "Groceries")
    response = _htmx(ledger, f"{MONTH}/transactions/{row_id}/delete")
    html = response.text
    assert response.status_code == 200 and _descriptions(html, "expense") == []
    assert 'hx-swap-oob="beforeend:#toast-container"' in html and "data-toast-strict" in html
    assert f'hx-post="{MONTH}/transactions/{row_id}/undo"' in html
    assert 'hx-target="closest [data-toast]"' in html and "Entry deleted." in html
    assert re.search(r'id="entry-expense-day"[^>]*data-autofocus', html)
    assert 'id="month-summary"' in html
    undo = _htmx(ledger, f"{MONTH}/transactions/{row_id}/undo")
    assert undo.status_code == 200
    assert '<section id="ledger-expense"' in undo.text and 'hx-swap-oob="outerHTML"' in undo.text
    assert _descriptions(undo.text, "expense") == ["Groceries"]
    assert "Entry restored." in undo.text and 'id="month-summary"' in undo.text
    assert _rows(ledger)[0].deleted_at is None


def test_undo_after_ten_seconds_is_too_late(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    _add(ledger, _form(food))
    row_id = _id(ledger, "Groceries")
    _htmx(ledger, f"{MONTH}/transactions/{row_id}/delete")
    ledger.app.state.clock = FixedClock("2026-10-14T00:30:11+00:00")
    response = _htmx(ledger, f"{MONTH}/transactions/{row_id}/undo")
    assert response.status_code == 409 and "Too late to undo" in response.text
    assert response.headers["HX-Retarget"] == "#toast-container"
    plain = ledger.post(f"{MONTH}/transactions/{row_id}/undo", token_from=MONTH)
    assert plain.headers["location"] == f"{MONTH}?error=undo_expired"
    assert "Too late to undo: the entry stays deleted." in _page(
        ledger, f"{MONTH}?error=undo_expired"
    )
    assert _rows(ledger)[0].deleted_at is not None


def test_delete_and_undo_work_without_javascript(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    _add(ledger, _form(food))
    row_id = _id(ledger, "Groceries")
    response = ledger.post(f"{MONTH}/transactions/{row_id}/delete", token_from=MONTH)
    assert response.headers["location"] == f"{MONTH}?deleted={row_id}#ledger-expense"
    page = _page(ledger, f"{MONTH}?deleted={row_id}")
    assert "data-deleted-notice" in page and f'action="{MONTH}/transactions/{row_id}/undo"' in page
    response = ledger.post(f"{MONTH}/transactions/{row_id}/undo", token_from=MONTH)
    assert response.headers["location"] == f"{MONTH}?notice=transaction_restored#tx-{row_id}"
    assert _descriptions(_page(ledger), "expense") == ["Groceries"]


def test_a_vanished_entry_reloads_the_page_with_a_message(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    _add(ledger, _form(food))
    row_id = _id(ledger, "Groceries")
    ledger.post(f"{MONTH}/transactions/{row_id}/delete", token_from=MONTH)
    response = _htmx(ledger, f"{MONTH}/transactions/{row_id}/delete")
    assert response.headers["HX-Redirect"] == f"{MONTH}?error=transaction_gone"
    data = {"day": "1", "amount": "1", "description": "", "category_id": str(food)}
    plain = ledger.post(f"{MONTH}/transactions/{row_id}", data, token_from=MONTH)
    assert plain.headers["location"] == f"{MONTH}?error=transaction_gone"
    assert "This entry no longer exists." in _page(ledger, f"{MONTH}?error=transaction_gone")
    assert _htmx_get(ledger, f"{MONTH}/transactions/{row_id}/edit").status_code == 404
    assert _htmx_get(ledger, f"{MONTH}/transactions/{UUID(int=1)}/edit").status_code == 404
    assert _htmx_get(ledger, f"{MONTH}/transactions/nonsense/edit").status_code == 404


# Autocomplete


def test_suggestions_are_options_with_their_category(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    _add(ledger, _form(food, description="Żabka <corner>"))
    _add(ledger, _form(food, description="Żabka <corner>"))
    response = ledger.client.get(
        "/transactions/suggestions",
        params={"direction": "expense", "description": "żA"},
        headers={"HX-Request": "true", "HX-Target": "entry-expense-suggestions"},
    )
    assert response.status_code == 200
    html = response.text
    assert html.count('role="option"') == 1
    assert 'id="entry-expense-suggestions-1"' in html and 'aria-selected="false"' in html
    assert f'data-category-id="{food}"' in html
    assert "Żabka &lt;corner&gt;" in html and "<corner>" not in html
    other = ledger.client.get(
        "/transactions/suggestions", params={"direction": "income", "description": "ż"}
    )
    assert 'role="option"' not in other.text
    empty = ledger.client.get(
        "/transactions/suggestions", params={"direction": "expense", "description": ""}
    )
    assert 'role="option"' not in empty.text
    bad = ledger.client.get("/transactions/suggestions", params={"direction": "x"})
    assert bad.status_code == 400
    odd = ledger.client.get(
        "/transactions/suggestions",
        params={"direction": "expense", "description": "Ż"},
        headers={"HX-Target": '"><script>'},
    )
    assert 'id="suggestions-1"' in odd.text


def test_the_description_field_is_a_combobox(ledger: OwnerApp) -> None:
    _category(ledger, "Food")
    entry = _section(_page(ledger), "expense")
    assert 'role="combobox" aria-autocomplete="list" aria-expanded="false"' in entry
    assert 'aria-controls="entry-expense-suggestions"' in entry
    assert 'hx-get="/transactions/suggestions?direction=expense"' in entry
    assert '<ul id="entry-expense-suggestions" role="listbox"' in entry


# Closed months and roles


def test_a_closed_month_is_read_only(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    _add(ledger, _form(food))
    _add(ledger, _form(food, description="Gone"))
    row_id, gone = _id(ledger, "Groceries"), _id(ledger, "Gone")
    assert ledger.post(f"{MONTH}/transactions/{gone}/delete", token_from=MONTH).status_code == 303
    assert ledger.post(f"{MONTH}/close", token_from=MONTH).status_code == 303
    page = _page(ledger)
    assert _descriptions(page, "expense") == ["Groceries"]
    for marker in ("entry-expense", "data-edit", "data-delete", ">Actions<"):
        assert marker not in page
    assert "data-edit-row" not in _page(ledger, f"{MONTH}?edit={row_id}")
    response = _add(ledger, _form(food))
    assert response.headers["location"] == f"{MONTH}?error=month_closed"
    htmx = _htmx(ledger, f"{MONTH}/transactions", _form(food))
    assert htmx.status_code == 409 and htmx.headers["HX-Redirect"] == f"{MONTH}?error=month_closed"
    data = {"day": "1", "amount": "1", "description": "", "category_id": str(food)}
    for path, form in (
        (f"{MONTH}/transactions/{row_id}", data),
        (f"{MONTH}/transactions/{row_id}/delete", {}),
        (f"{MONTH}/transactions/{gone}/undo", {}),
    ):
        assert _htmx(ledger, path, form).headers["HX-Redirect"] == f"{MONTH}?error=month_closed"
    assert "This month is closed. Its entries and planned values cannot change" in _page(
        ledger, f"{MONTH}?error=month_closed"
    )
    assert [r.deleted_at is None for r in _rows(ledger)] == [True, False]


def test_a_full_date_in_another_closed_month_is_a_day_error(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    assert ledger.post("/m/2026-09/close", token_from="/m/2026-09").status_code == 303
    response = _htmx(ledger, f"{MONTH}/transactions", _form(food, "2026-09-30"))
    assert response.status_code == 409
    assert "September 2026 is closed. Choose a date in an open month." in response.text
    assert 'value="2026-09-30"' in response.text and "HX-Redirect" not in response.headers
    _add(ledger, _form(food))
    row_id = _id(ledger, "Groceries")
    data = {"day": "01.09.2026", "amount": "1", "description": "", "category_id": str(food)}
    response = ledger.post(f"{MONTH}/transactions/{row_id}", data, token_from=MONTH)
    assert response.status_code == 409 and "September 2026 is closed." in response.text
    assert "data-edit-row" in response.text


def test_viewers_see_the_tables_without_controls_and_cannot_write(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    _add(ledger, _form(food))
    row_id = _id(ledger, "Groceries")
    ledger.set_admin_role("viewer")
    page = _page(ledger)
    assert _descriptions(page, "expense") == ["Groceries"]
    for marker in ("entry-expense", "data-edit", "data-delete", "Enter saves"):
        assert marker not in page
    assert "data-edit-row" not in _page(ledger, f"{MONTH}?edit={row_id}")
    row = _htmx_get(ledger, f"{MONTH}/transactions/{row_id}/edit").text
    assert "data-edit-row" not in row and "data-edit" not in row
    for path, form in (
        (f"{MONTH}/transactions", _form(food)),
        (f"{MONTH}/transactions/{row_id}", _form(food)),
        (f"{MONTH}/transactions/{row_id}/delete", {}),
        (f"{MONTH}/transactions/{row_id}/undo", {}),
    ):
        response = ledger.post(path, form, token_from=MONTH)
        assert response.status_code == 403 and NO_PERMISSION in response.text
    assert len(_rows(ledger)) == 1


def test_editors_can_write(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    ledger.set_admin_role("editor")
    assert "entry-expense" in _page(ledger)
    assert _add(ledger, _form(food)).status_code == 303


def test_writes_need_the_csrf_token(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    response = ledger.client.post(f"{MONTH}/transactions", data=_form(food), follow_redirects=False)
    assert response.status_code == 403 and _rows(ledger) == []


def test_another_workspaces_entry_is_not_found(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    _add(ledger, _form(food))
    row_id = _id(ledger, "Groceries")
    other = ledger.add_workspace("Other")
    ledger.join(ledger.admin_id, other, "owner")
    token = ledger.token("/")
    switch = {"workspace_id": str(other), "csrf_token": token}
    assert ledger.client.post("/workspace/switch", data=switch).status_code == 200
    assert _htmx_get(ledger, f"{MONTH}/transactions/{row_id}/edit").status_code == 404
    response = ledger.post(f"{MONTH}/transactions/{row_id}/delete", token_from=MONTH)
    assert response.headers["location"] == f"{MONTH}?error=transaction_gone"
    assert _rows(ledger)[0].deleted_at is None


def test_the_page_has_no_inline_scripts_or_styles(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    _add(ledger, _form(food))
    row_id = _id(ledger, "Groceries")
    for html in (
        _page(ledger, f"{MONTH}?edit={row_id}"),
        _htmx(ledger, f"{MONTH}/transactions/{row_id}/delete").text,
    ):
        assert not re.search(r"<script(?![^>]*\bsrc=)", html)
        assert " style=" not in html and "<style" not in html
        assert not re.search(r"\s(x-|@|:)[a-z]", html)


def test_each_table_keeps_its_own_day_and_another_months_day_stays_as_typed(
    ledger: OwnerApp,
) -> None:
    food = _category(ledger, "Food")
    salary = _category(ledger, "Salary", "income")
    html = _htmx(ledger, f"{MONTH}/transactions", _form(food, "2026-09-03")).text
    assert re.search(r'id="entry-expense-day"[^>]*value="2026-09-03"', html)
    income = _form(salary, "25", direction="income")
    html = _htmx(ledger, f"{MONTH}/transactions", income).text
    assert re.search(r'id="entry-income-day"[^>]*value="25"', html)
    assert 'id="entry-expense-day"' not in html  # only the income table is returned


def test_the_kept_day_prefills_the_page_without_javascript(ledger: OwnerApp) -> None:
    _category(ledger, "Food")
    _category(ledger, "Salary", "income")
    page = _page(ledger, f"{MONTH}?notice=transaction_added&entry=income&day=12")
    assert re.search(r'id="entry-income-day"[^>]*value="12"', page)
    assert re.search(r'id="entry-expense-day"[^>]*value=""', page)
    for query in ("entry=income&day=%3Cb%3E", "entry=income&day=123", "entry=x&day=12"):
        page = _page(ledger, f"{MONTH}?{query}")
        assert re.search(r'id="entry-income-day"[^>]*value=""', page), query
        assert "<b>" not in page


def test_the_undo_countdown_is_the_time_the_server_still_allows(
    ledger: OwnerApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from assetboard.services import transactions as service
    from assetboard.web import transaction_routes

    food = _category(ledger, "Food")
    _add(ledger, _form(food))
    row_id = _id(ledger, "Groceries")
    real_delete = service.delete_transaction

    def slow_delete(*args: object) -> service.DeletedTransaction:
        deleted = real_delete(*args)  # type: ignore[arg-type]
        return service.DeletedTransaction(
            deleted.id, deleted.month, deleted.undo_until - timedelta(seconds=3, milliseconds=500)
        )

    monkeypatch.setattr(transaction_routes, "delete_transaction", slow_delete)
    html = _htmx(ledger, f"{MONTH}/transactions/{row_id}/delete").text
    assert 'data-toast-strict data-toast-seconds="7"' in html
    assert "data-toast-countdown>7</span>" in html


def test_undo_is_offered_without_javascript_only_for_a_pending_deletion(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    _add(ledger, _form(food))
    _add(ledger, _form(food, "2026-09-05", description="September"))
    row_id, september = _id(ledger, "Groceries"), _id(ledger, "September")
    assert "data-deleted-notice" not in _page(ledger, f"{MONTH}?deleted={row_id}")  # live row
    ledger.post(f"{MONTH}/transactions/{row_id}/delete", token_from=MONTH)
    ledger.post(f"/m/2026-09/transactions/{september}/delete", token_from="/m/2026-09")
    assert "data-deleted-notice" in _page(ledger, f"{MONTH}?deleted={row_id}")
    assert "data-deleted-notice" not in _page(ledger, f"{MONTH}?deleted={september}")  # other month
    assert "data-deleted-notice" not in _page(ledger, f"{MONTH}?deleted={UUID(int=3)}")
    ledger.app.state.clock = FixedClock("2026-10-14T00:30:11+00:00")
    assert "data-deleted-notice" not in _page(ledger, f"{MONTH}?deleted={row_id}")  # too late


def test_after_a_delete_without_an_entry_row_the_heading_takes_the_focus(
    ledger: OwnerApp,
) -> None:
    food = _category(ledger, "Food")
    _add(ledger, _form(food))
    row_id = _id(ledger, "Groceries")
    assert ledger.post(f"/categories/{food}/archive", token_from="/categories").status_code == 303
    html = _htmx(ledger, f"{MONTH}/transactions/{row_id}/delete").text
    assert "Add an expense category first." in html
    assert (
        'id="ledger-expense-heading" class="text-base font-semibold" tabindex="-1" data-autofocus'
        in html
    )


def test_the_description_limit_comes_from_the_domain(ledger: OwnerApp) -> None:
    from assetboard.domain.transactions import MAX_DESCRIPTION_LENGTH

    _category(ledger, "Food")
    assert f'maxlength="{MAX_DESCRIPTION_LENGTH}"' in _section(_page(ledger), "expense")
