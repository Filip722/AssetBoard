"""The transaction list over HTTP (F2.6): filters, sorting, pages, the CSV export, bulk
recategorize with its confirmation page, roles, CSRF and translations."""

from __future__ import annotations

import csv
import io
import re
from tempfile import SpooledTemporaryFile
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID, uuid4

import anyio
import pytest
from babel.messages.pofile import read_po
from sqlalchemy import select
from starlette.requests import ClientDisconnect

from assetboard.clock import FixedClock
from assetboard.db.models.identity import AuditEvent
from assetboard.db.models.ledger import Category, Transaction
from assetboard.i18n import gettext
from assetboard.services import transaction_search as search_service
from assetboard.services.transaction_search import MAX_SELECTED_IDS
from assetboard.web import transaction_list_routes as routes
from tests.conftest import OwnerApp

NOW = "2026-10-14T00:30:00+00:00"
NO_PERMISSION = "You do not have permission to view this page."


@pytest.fixture
def ledger(owner: OwnerApp) -> OwnerApp:
    owner.app.state.clock = FixedClock(NOW)
    owner.clear_categories()  # these tests name their own categories
    return owner


def _category(app: OwnerApp, name: str, kind: str = "expense") -> UUID:
    form = {"name": name, "kind": kind, "treatment": "regular", "color": "#16a34a"}
    response = app.post("/categories", form, token_from="/categories")
    assert response.status_code == 303, response.text
    with app.db() as session:
        return session.scalars(
            select(Category.id).where(Category.name == name, Category.kind == kind)
        ).one()


def _add(
    app: OwnerApp,
    category: UUID,
    day: str,
    amount: str = "12.50",
    description: str = "Groceries",
    direction: str = "expense",
) -> UUID:
    month = f"/m/{day[:7]}"
    form = {
        "direction": direction,
        "day": day,
        "amount": amount,
        "description": description,
        "category_id": str(category),
    }
    response = app.post(f"{month}/transactions", form, token_from=month)
    assert response.status_code == 303, response.text
    with app.db() as session:
        return session.scalars(
            select(Transaction.id).order_by(Transaction.created_at.desc()).limit(1)
        ).one()


def _page(app: OwnerApp, query: str = "", status: int = 200) -> str:
    response = app.client.get(f"/transactions{query}")
    assert response.status_code == status, response.text
    return response.text


def _descriptions(html: str) -> list[str]:
    return re.findall(r"data-description>([^<]*)<", html)


def _hidden(html: str, form_id: str = "bulk-form") -> list[tuple[str, str]]:
    match = re.search(rf'<form id="{form_id}".*?</form>', html, re.DOTALL)
    assert match is not None
    pairs = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', match.group(0))
    return [(name, value) for name, value in pairs if name != "csrf_token"]


@pytest.fixture
def rows(ledger: OwnerApp) -> dict[str, UUID]:
    food, salary = _category(ledger, "Food"), _category(ledger, "Salary", "income")
    return {
        "food": food,
        "salary": salary,
        "aug": _add(ledger, food, "2026-08-31", "5", "Bakery"),
        "sep": _add(ledger, food, "2026-09-01", "1234.56", "Żabka weekly"),
        "oct": _add(ledger, food, "2026-10-01", "12.50", "Lidl"),
        "pay": _add(ledger, salary, "2026-09-30", "3400", "Pay", "income"),
    }


# The page


def test_the_list_shows_every_month_newest_first(ledger: OwnerApp, rows: dict) -> None:
    html = _page(ledger)
    assert _descriptions(html) == ["Lidl", "Pay", "Żabka weekly", "Bakery"]
    assert "1–4 of 4 entries" in html
    assert 'href="/m/2026-09"' in html  # each date links to its month
    assert "€1,234.56" in html
    assert "data-transactions-link" in html  # in the main navigation


def test_filters_in_the_query_string(ledger: OwnerApp, rows: dict) -> None:
    html = _page(ledger, "?from=2026-09-01&to=2026-09-30&direction=expense")
    assert _descriptions(html) == ["Żabka weekly"]
    html = _page(ledger, f"?category={rows['salary']}&category={rows['food']}&q=a&min=6")
    assert _descriptions(html) == ["Pay", "Żabka weekly"]
    assert _descriptions(_page(ledger, "?q=LIDL")) == ["Lidl"]
    assert _descriptions(_page(ledger, "?max=12.5")) == ["Lidl", "Bakery"]
    assert "No entries match these filters." in _page(ledger, "?q=nothing")


def test_sorting(ledger: OwnerApp, rows: dict) -> None:
    assert _descriptions(_page(ledger, "?sort=amount")) == ["Bakery", "Lidl", "Żabka weekly", "Pay"]
    assert _descriptions(_page(ledger, "?sort=-amount"))[0] == "Pay"
    assert _descriptions(_page(ledger, "?sort=date"))[0] == "Bakery"
    assert _descriptions(_page(ledger, "?sort=nonsense"))[0] == "Lidl"  # the default


def test_pages_of_fifty_keep_the_filters(ledger: OwnerApp) -> None:
    food = _category(ledger, "Food")
    for n in range(51):
        _add(ledger, food, f"2026-09-{1 + n % 28:02d}", str(n + 1), f"row {n}")
    html = _page(ledger, "?q=row&sort=amount")
    assert len(_descriptions(html)) == 50
    assert "1–50 of 51 entries" in html and "Page 1 of 2" in html
    next_url = re.search(r'href="([^"]+)" rel="next"', html)
    assert next_url is not None
    query = dict(parse_qsl(urlsplit(next_url.group(1).replace("&amp;", "&")).query))
    assert query == {"q": "row", "sort": "amount", "page": "2"}
    second = _page(ledger, "?q=row&sort=amount&page=2")
    assert _descriptions(second) == ["row 50"] and "51–51 of 51 entries" in second
    assert 'rel="prev"' in second and 'rel="next"' not in second
    assert "Page 2 of 2" in _page(ledger, "?q=row&sort=amount&page=99")  # clamped


def test_invalid_filters_are_marked_and_show_no_rows(ledger: OwnerApp, rows: dict) -> None:
    html = _page(ledger, "?from=7&min=abc&to=2026-02-30")
    assert _descriptions(html) == []
    assert "Correct the marked filters to see the entries." in html
    assert 'id="filter-date_from-error"' in html and 'aria-invalid="true"' in html
    assert "Enter a valid amount." in html
    assert "That month has no such day." in html
    assert 'value="abc"' in html  # what was typed stays


def test_polish_amounts_and_page(ledger: OwnerApp, rows: dict) -> None:
    ledger.client.headers["Accept-Language"] = "pl"
    response = ledger.client.get("/transactions?min=1 234,56")
    assert response.status_code == 200
    assert set(_descriptions(response.text)) == {"Pay", "Żabka weekly"}


def test_rows_in_closed_months_are_marked(ledger: OwnerApp, rows: dict) -> None:
    response = ledger.post("/m/2026-09/close", {}, token_from="/m/2026-09")
    assert response.status_code == 303
    assert len(re.findall(r"data-closed>", _page(ledger))) == 2


def test_viewers_list_and_export_without_bulk_controls(ledger: OwnerApp, rows: dict) -> None:
    ledger.set_admin_role("viewer")
    html = _page(ledger)
    assert len(_descriptions(html)) == 4
    assert 'id="bulk-form"' not in html and 'name="ids"' not in html
    assert "data-export-form" in html
    response = ledger.client.get("/transactions/export.csv")
    assert response.status_code == 200
    data = {"scope": "selected", "ids": str(rows["sep"]), "category_id": str(rows["food"])}
    for path in ("/transactions/recategorize", "/transactions/recategorize/confirm"):
        refused = ledger.post(path, data, token_from="/transactions")
        assert refused.status_code == 403
        assert NO_PERMISSION in refused.text


# Export


def test_the_export_downloads_the_filtered_rows(ledger: OwnerApp, rows: dict) -> None:
    response = ledger.client.get("/transactions/export.csv?direction=expense&sort=-amount")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == (
        'attachment; filename="assetboard-transactions-20261014-003000Z.csv"'
    )
    text = response.content.decode("utf-8")
    parsed = list(csv.reader(io.StringIO(text, newline="")))
    assert parsed[0] == [
        "date", "direction", "amount", "currency", "category", "description", "category_id", "id",
    ]  # fmt: skip
    # Entry order (the archive's), not the screen's sort.
    assert [r[0] for r in parsed[1:]] == ["2026-08-31", "2026-09-01", "2026-10-01"]
    assert parsed[2] == [
        "2026-09-01", "expense", "1234.56", "EUR", "Food", "Żabka weekly", str(rows["food"]),
        str(rows["sep"]),
    ]  # fmt: skip


def test_export_options(ledger: OwnerApp, rows: dict) -> None:
    response = ledger.client.get(
        "/transactions/export.csv?q=lidl&delimiter=semicolon&decimal=comma&bom=1"
    )
    assert response.content.startswith(b"\xef\xbb\xbfdate;direction;amount;")
    assert b";12,50;EUR;Food;Lidl;" in response.content


def test_the_formula_guard_is_opt_in(ledger: OwnerApp, rows: dict) -> None:
    food = rows["food"]
    _add(ledger, food, "2026-10-02", "1", "=HYPERLINK(1)")
    plain = ledger.client.get("/transactions/export.csv?q=hyperlink").content
    guarded = ledger.client.get("/transactions/export.csv?q=hyperlink&guard=1").content
    assert b",=HYPERLINK(1)," in plain
    assert b",'=HYPERLINK(1)," in guarded
    html = _page(ledger)
    assert re.search(r'name="guard" value="1"(?! checked)', html)  # unticked by default


def test_invalid_export_options_return_the_page(ledger: OwnerApp, rows: dict) -> None:
    response = ledger.client.get("/transactions/export.csv?decimal=comma")
    assert response.status_code == 400
    assert "A decimal comma needs the semicolon delimiter, so that amounts stay one column." in (
        response.text
    )
    response = ledger.client.get("/transactions/export.csv?min=abc")
    assert response.status_code == 400 and "Enter a valid amount." in response.text


def test_the_export_form_carries_the_filters(ledger: OwnerApp, rows: dict) -> None:
    html = _page(ledger, f"?q=a&category={rows['food']}&sort=amount&page=1")
    match = re.search(r"<form method=\"get\" action=\"/transactions/export.csv\".*?</form>", html,
                      re.DOTALL)  # fmt: skip
    assert match is not None
    hidden = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', match.group(0))
    assert hidden == [("category", str(rows["food"])), ("q", "a"), ("sort", "amount")]


def test_the_export_is_not_audited(ledger: OwnerApp, rows: dict) -> None:
    with ledger.db() as session:
        before = len(session.scalars(select(AuditEvent)).all())
    assert ledger.client.get("/transactions/export.csv").status_code == 200
    with ledger.db() as session:
        assert len(session.scalars(select(AuditEvent)).all()) == before


# Bulk recategorize


def _category_of(app: OwnerApp, row_id: UUID) -> UUID:
    with app.db() as session:
        row = session.get(Transaction, row_id)
        assert row is not None
        return row.category_id


def _confirm_fields(html: str) -> dict[str, Any]:
    match = re.search(r'<form method="post" action="/transactions/recategorize/confirm".*?</form>',
                      html, re.DOTALL)  # fmt: skip
    assert match is not None
    pairs = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', match.group(0))
    return {"pairs": [(k, v) for k, v in pairs if k != "csrf_token"]}


def _post_pairs(app: OwnerApp, path: str, pairs: list[tuple[str, str]]) -> Any:
    data: dict[str, list[str]] = {"csrf_token": [app.token("/transactions")]}
    for name, value in pairs:
        data.setdefault(name, []).append(value)
    return app.client.post(path, data=data, follow_redirects=False)


def test_recategorize_ticked_rows_with_confirmation(ledger: OwnerApp, rows: dict) -> None:
    groceries = _category(ledger, "Groceries")
    html = _page(ledger, "?direction=expense")
    assert 'name="ids" value="' in html and 'form="bulk-form"' in html
    assert _hidden(html) == [("direction", "expense")]
    pairs = [
        ("direction", "expense"),
        ("ids", str(rows["aug"])),
        ("ids", str(rows["sep"])),
        ("category_id", str(groceries)),
        ("scope", "selected"),
    ]
    preview = _post_pairs(ledger, "/transactions/recategorize", pairs)
    assert preview.status_code == 200
    assert "Move 2 entries to Groceries?" in preview.text
    assert "August 2026, September 2026" in preview.text
    assert _category_of(ledger, rows["aug"]) == rows["food"]  # nothing changed yet

    confirm = _confirm_fields(preview.text)["pairs"]
    assert ("scope", "selected") in confirm
    assert any(name == "fingerprint" for name, _value in confirm)
    done = _post_pairs(ledger, "/transactions/recategorize/confirm", confirm)
    assert done.status_code == 303
    assert done.headers["location"] == "/transactions?direction=expense&changed=2"
    assert _category_of(ledger, rows["aug"]) == groceries
    assert _category_of(ledger, rows["sep"]) == groceries
    assert _category_of(ledger, rows["oct"]) == rows["food"]
    assert "Category changed for 2 entries." in _page(ledger, "?direction=expense&changed=2")
    with ledger.db() as session:
        (event,) = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "transaction.recategorized")
        ).all()
        assert event.data["count"] == 2


def test_recategorize_all_matching_rows(ledger: OwnerApp, rows: dict) -> None:
    other = _category(ledger, "Other")
    pairs = [("q", "a"), ("direction", "expense"), ("category_id", str(other)),
             ("scope", "filtered")]  # fmt: skip
    preview = _post_pairs(ledger, "/transactions/recategorize", pairs)
    assert "Move 2 entries to Other?" in preview.text
    done = _post_pairs(ledger, "/transactions/recategorize/confirm",
                       _confirm_fields(preview.text)["pairs"])  # fmt: skip
    assert done.status_code == 303
    assert _category_of(ledger, rows["aug"]) == other  # "Bakery"
    assert _category_of(ledger, rows["sep"]) == other  # "Żabka weekly"
    assert _category_of(ledger, rows["oct"]) == rows["food"]


def test_a_closed_month_refuses_the_whole_change(ledger: OwnerApp, rows: dict) -> None:
    other = _category(ledger, "Other")
    pairs = [("direction", "expense"), ("category_id", str(other)), ("scope", "filtered")]
    preview = _post_pairs(ledger, "/transactions/recategorize", pairs)
    confirm = _confirm_fields(preview.text)["pairs"]
    assert ledger.post("/m/2026-09/close", {}, token_from="/m/2026-09").status_code == 303
    refused = _post_pairs(ledger, "/transactions/recategorize/confirm", confirm)
    assert refused.status_code == 409
    assert (
        "Nothing was changed: some entries are in closed months (September 2026). An owner can "
        "reopen them." in refused.text
    )
    assert {_category_of(ledger, rows[k]) for k in ("aug", "sep", "oct")} == {rows["food"]}
    # The preview refuses too.
    again = _post_pairs(ledger, "/transactions/recategorize", pairs)
    assert again.status_code == 409 and "data-bulk-error" in again.text


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"scope": "selected", "ids": ""}, "Select at least one entry."),
        ({"category_id": "not-a-uuid"}, "Choose an active category."),
        ({"target": "salary"}, "Choose a category of the same kind as the entries."),
    ],
)
def test_bulk_refusals(ledger: OwnerApp, rows: dict, change: dict, message: str) -> None:
    pairs = {"scope": "selected", "ids": str(rows["sep"]), "category_id": str(rows["food"])}
    if change.pop("target", None):
        pairs["category_id"] = str(rows["salary"])
    pairs |= change
    response = _post_pairs(ledger, "/transactions/recategorize", list(pairs.items()))
    assert response.status_code == 400
    assert message in response.text
    assert "data-bulk-error" in response.text


def test_a_changed_selection_is_refused(ledger: OwnerApp, rows: dict) -> None:
    other = _category(ledger, "Other")
    pairs = [("direction", "expense"), ("category_id", str(other)), ("scope", "filtered")]
    preview = _post_pairs(ledger, "/transactions/recategorize", pairs)
    _add(ledger, rows["food"], "2026-10-05", "1", "New one")
    refused = _post_pairs(ledger, "/transactions/recategorize/confirm",
                          _confirm_fields(preview.text)["pairs"])  # fmt: skip
    assert refused.status_code == 409
    assert "The entries changed after you reviewed the change." in refused.text


def test_bulk_posts_need_csrf(ledger: OwnerApp, rows: dict) -> None:
    data = {"scope": "selected", "ids": str(rows["sep"]), "category_id": str(rows["food"])}
    for path in ("/transactions/recategorize", "/transactions/recategorize/confirm"):
        response = ledger.client.post(path, data=data, follow_redirects=False)
        assert response.status_code == 403


def test_change_all_is_disabled_above_the_cap(
    ledger: OwnerApp, rows: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = _page(ledger, "?direction=expense")
    assert re.search(r'value="filtered" class="[^"]*" data-bulk-filtered>', html)  # enabled
    assert "data-bulk-too-many" not in html
    monkeypatch.setattr(search_service, "MAX_RECATEGORIZE_ROWS", 2)
    html = _page(ledger, "?direction=expense")  # 3 matching rows
    button = re.search(r'<button[^>]*value="filtered"[^>]*>', html)
    assert button is not None
    assert " disabled " in button.group(0)
    assert 'aria-describedby="bulk-too-many"' in button.group(0)
    assert (
        '<p id="bulk-too-many" class="mt-2 text-xs text-muted" data-bulk-too-many>Too many '
        "entries for one change (at most 2). Narrow the filters first.</p>"
    ) in html
    # Ticked rows can still be changed.
    assert "data-bulk-selected" in html


def test_too_many_ticked_entries_have_their_own_refusal(ledger: OwnerApp, rows: dict) -> None:
    ids = [("ids", str(uuid4())) for _ in range(MAX_SELECTED_IDS + 1)]
    pairs = [*ids, ("category_id", str(rows["food"])), ("scope", "selected")]
    response = _post_pairs(ledger, "/transactions/recategorize", pairs)
    assert response.status_code == 400
    assert (
        f"Too many ticked entries (at most {MAX_SELECTED_IDS} at a time). Tick fewer, or change "
        "all matching entries." in response.text
    )


def test_the_results_table_has_a_caption(ledger: OwnerApp, rows: dict) -> None:
    assert '<caption class="sr-only">Entries matching the filters</caption>' in _page(ledger)


def _run(response: Any, scope: dict[str, Any], receive: Any, send: Any) -> None:
    async def call() -> None:
        await response(scope, receive, send)

    anyio.run(call)


class _Disconnect(OSError):
    pass


def test_the_spool_is_closed_when_sending_fails_mid_stream() -> None:
    spool: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(max_size=1024)  # noqa: SIM115
    spool.write(b"x" * (routes.EXPORT_CHUNK_BYTES * 3))
    spool.seek(0)
    response = routes.SpoolResponse(spool, media_type="text/csv")
    sent: list[str] = []

    async def receive() -> dict[str, Any]:
        await anyio.sleep_forever()
        raise AssertionError  # pragma: no cover

    async def send(message: dict[str, Any]) -> None:
        sent.append(message["type"])
        if message["type"] == "http.response.body" and len(sent) > 2:
            raise _Disconnect("client went away")

    scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "GET"}
    # Starlette reports a failed send as ClientDisconnect (ASGI 2.4) or re-raises it.
    with pytest.raises((_Disconnect, ClientDisconnect, BaseExceptionGroup)):
        _run(response, scope, receive, send)
    assert spool.closed
    assert sent.count("http.response.body") == 2  # stopped mid-stream


def test_the_spool_is_closed_after_a_complete_download() -> None:
    spool: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(max_size=1024)  # noqa: SIM115
    spool.write(b"a,b\r\n")
    spool.seek(0)
    response = routes.SpoolResponse(spool, media_type="text/csv")
    body: list[bytes] = []

    async def receive() -> dict[str, Any]:
        await anyio.sleep_forever()
        raise AssertionError  # pragma: no cover

    async def send(message: dict[str, Any]) -> None:
        body.append(message.get("body", b""))

    _run(
        response, {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "GET"}, receive, send
    )
    assert b"".join(body) == b"a,b\r\n"
    assert spool.closed


# Translations


def test_every_message_has_a_polish_translation() -> None:
    texts = [
        *routes.FILTER_ERRORS.values(),
        *routes.OPTION_ERRORS.values(),
        *routes.BULK_ERRORS.values(),
        *routes.SORT_LABELS.values(),
        *routes.DIRECTION_LABELS.values(),
        routes.FILTER_INVALID,
    ]
    for text in texts:
        assert gettext(text, "pl") != text, f"no Polish translation for {text!r}"


def test_the_page_is_translated_to_polish(ledger: OwnerApp, rows: dict) -> None:
    with open("src/assetboard/locales/pl/LC_MESSAGES/messages.po", "rb") as handle:
        catalog = read_po(handle)
    for message_id in (
        "Transactions",
        "Export as CSV",
        "Change category",
        "Protect against spreadsheet formulas",
    ):
        message = catalog.get(message_id)
        assert message is not None and message.string and not message.fuzzy, message_id
