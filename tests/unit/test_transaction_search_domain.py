"""Transaction search and CSV rows (F2.6): the filter, the page window and the CSV format."""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.ledger_csv import (
    BOM,
    FORMULA_GUARD_DEFAULT,
    TRANSACTION_EXPORT_COLUMNS,
    CsvOptionError,
    CsvOptions,
    TransactionCsvRow,
    guard_formula,
    plain_amount,
    transactions_csv,
)
from assetboard.domain.transaction_search import (
    DEFAULT_SORT,
    PAGE_SIZE,
    SortOrder,
    TransactionFilter,
    page_window,
    parse_sort,
    search_key,
)

FOOD = UUID("2b0e8c1e-5f7a-4c6e-9a51-3f1d2c7b9e01")
ROW_ID = UUID("5c7d8e9f-0a1b-4c2d-9e3f-4a5b6c7d8e02")


def _row(**changes: object) -> TransactionCsvRow:
    values: dict[str, object] = {
        "date": date(2026, 9, 2),
        "direction": "expense",
        "amount_minor": 5000,
        "currency": "EUR",
        "category": "Food",
        "description": "Groceries",
        "category_id": FOOD,
        "id": ROW_ID,
    }
    values |= changes
    return TransactionCsvRow(**values)  # type: ignore[arg-type]


def _text(rows: list[TransactionCsvRow], options: CsvOptions | None = None) -> str:
    return "".join(transactions_csv(rows, options or CsvOptions()))


# Filter, sort, pages


def test_page_size_is_fifty_and_the_default_sort_is_newest_first() -> None:
    assert PAGE_SIZE == 50
    assert DEFAULT_SORT is SortOrder.DATE_DESC


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("-date", SortOrder.DATE_DESC),
        ("date", SortOrder.DATE_ASC),
        ("-amount", SortOrder.AMOUNT_DESC),
        ("amount", SortOrder.AMOUNT_ASC),
        ("", SortOrder.DATE_DESC),
        ("description", SortOrder.DATE_DESC),
    ],
)
def test_parse_sort_falls_back_to_the_default(value: str, expected: SortOrder) -> None:
    assert parse_sort(value) is expected


def test_a_filter_refuses_reversed_ranges() -> None:
    with pytest.raises(ValueError, match="date_from"):
        TransactionFilter(date_from=date(2026, 10, 2), date_to=date(2026, 10, 1))
    with pytest.raises(ValueError, match="amount_min"):
        TransactionFilter(amount_min_minor=200, amount_max_minor=100)
    same_day = TransactionFilter(date_from=date(2026, 10, 1), date_to=date(2026, 10, 1))
    assert same_day.date_from == same_day.date_to


def test_search_key_folds_like_the_description_key() -> None:
    assert search_key("  ŻABKA   Shop ") == "żabka shop"
    assert search_key("Straße") == "strasse"


@pytest.mark.parametrize(
    ("requested", "total", "number", "pages", "first", "last"),
    [
        (1, 0, 1, 1, 0, 0),
        (1, 1, 1, 1, 1, 1),
        (1, 50, 1, 1, 1, 50),
        (2, 50, 1, 1, 1, 50),  # past the end: the last page
        (2, 51, 2, 2, 51, 51),
        (3, 101, 3, 3, 101, 101),
        (0, 101, 1, 3, 1, 50),  # below 1: the first page
        (-4, 101, 1, 3, 1, 50),
        (99, 120, 3, 3, 101, 120),
    ],
)
def test_page_window(
    requested: int, total: int, number: int, pages: int, first: int, last: int
) -> None:
    window = page_window(requested, total)
    assert (window.number, window.pages, window.first_item, window.last_item) == (
        number,
        pages,
        first,
        last,
    )
    assert window.offset == (number - 1) * 50
    assert window.has_previous == (number > 1)
    assert window.has_next == (number < pages)


@given(st.integers(min_value=-5, max_value=500), st.integers(min_value=0, max_value=10_000))
def test_a_page_window_always_lies_within_the_results(requested: int, total: int) -> None:
    window = page_window(requested, total)
    assert 1 <= window.number <= window.pages
    assert 0 <= window.offset <= max(total - 1, 0)
    assert window.last_item - window.first_item + 1 <= PAGE_SIZE or total == 0


# CSV


def test_the_columns_are_m4_transactions_csv_without_deleted_at() -> None:
    # docs/specs/M4-ledger-export-import.md, "transactions.csv": the same names and order.
    archive = "date,direction,amount,currency,category,description,category_id,id,deleted_at"
    assert tuple(archive.split(",")[:-1]) == TRANSACTION_EXPORT_COLUMNS


def test_default_options_write_a_canonical_transactions_csv() -> None:
    rows = [
        _row(),
        _row(description="Bakery, rolls", amount_minor=420),
        _row(direction="income", category="Salary", description="", amount_minor=340000),
    ]
    text = _text(rows)
    assert text == (
        "date,direction,amount,currency,category,description,category_id,id\r\n"
        f"2026-09-02,expense,50.00,EUR,Food,Groceries,{FOOD},{ROW_ID}\r\n"
        f'2026-09-02,expense,4.20,EUR,Food,"Bakery, rolls",{FOOD},{ROW_ID}\r\n'
        f"2026-09-02,income,3400.00,EUR,Salary,,{FOOD},{ROW_ID}\r\n"
    )
    assert not text.startswith(BOM)


def test_quotes_and_line_breaks_are_quoted_per_rfc_4180() -> None:
    text = _text([_row(description='Say "hi"; ok')])
    assert '"Say ""hi""; ok"' in text  # the quote forces quoting; ";" alone would not
    parsed = list(csv.reader(io.StringIO(text, newline="")))
    assert parsed[1][5] == 'Say "hi"; ok'


def test_semicolon_and_decimal_comma() -> None:
    options = CsvOptions(delimiter="semicolon", decimal="comma")
    text = _text([_row(amount_minor=1250, description="Rolls; fresh")], options)
    lines = text.split("\r\n")
    assert lines[0] == "date;direction;amount;currency;category;description;category_id;id"
    assert lines[1] == f'2026-09-02;expense;12,50;EUR;Food;"Rolls; fresh";{FOOD};{ROW_ID}'
    # Semicolon with a dot is offered too.
    dotted = _text([_row(amount_minor=1250)], CsvOptions(delimiter="semicolon"))
    assert ";12.50;" in dotted


def test_the_bom_comes_once_before_the_header() -> None:
    text = _text([_row()], CsvOptions(bom=True))
    assert text.startswith(BOM + "date,direction,")
    assert text.count(BOM) == 1


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"delimiter": "tab"}, "invalid_delimiter"),
        ({"decimal": "space"}, "invalid_decimal"),
        ({"decimal": "comma"}, "decimal_comma_needs_semicolon"),
    ],
)
def test_unsupported_options_are_refused(changes: dict[str, str], code: str) -> None:
    with pytest.raises(CsvOptionError) as caught:
        CsvOptions(**changes)  # type: ignore[arg-type]
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("minor", "currency", "decimal", "text"),
    [
        (1250, "EUR", "dot", "12.50"),
        (1250, "EUR", "comma", "12,50"),
        (1, "EUR", "dot", "0.01"),
        (100, "EUR", "dot", "1.00"),
        (123456789, "EUR", "dot", "1234567.89"),  # no grouping
        (1200, "JPY", "dot", "1200"),  # no minor digits
        (1200, "JPY", "comma", "1200"),
        (1234, "BHD", "dot", "1.234"),  # three minor digits
        (10**15 - 1, "EUR", "dot", "9999999999999.99"),
    ],
)
def test_plain_amount(minor: int, currency: str, decimal: str, text: str) -> None:
    assert plain_amount(minor, currency, decimal=decimal) == text


@given(st.integers(min_value=1, max_value=10**15 - 1))
def test_plain_amount_round_trips_through_decimal(minor: int) -> None:
    text = plain_amount(minor, "EUR")
    assert Decimal(text) * 100 == minor
    assert len(text.split(".")[1]) == 2


def test_the_formula_guard_is_off_by_default() -> None:
    assert FORMULA_GUARD_DEFAULT is False
    assert CsvOptions().formula_guard is False
    assert "=SUM(A1)" in _text([_row(description="=SUM(A1)")])


@pytest.mark.parametrize(
    ("value", "guarded"),
    [
        ("=HYPERLINK(1)", "'=HYPERLINK(1)"),
        ("+48 123", "'+48 123"),
        ("-5 refund", "'-5 refund"),
        ("@sum", "'@sum"),
        ("\tx", "'\tx"),
        ("\rx", "'\rx"),
        ("Lidl", "Lidl"),
        ("a=b", "a=b"),
        ("a-b", "a-b"),
        ("e-mail @ home", "e-mail @ home"),
        ("", ""),
        # Full-width triggers
        ("\uff1dSUM(1)", "'\uff1dSUM(1)"),
        ("\uff0b48", "'\uff0b48"),
        ("\uff0d5", "'\uff0d5"),
        ("\uff20x", "'\uff20x"),
        # Leading whitespace before a trigger
        (" =1", "' =1"),
        ("  -5", "'  -5"),
        ("\u3000=1", "'\u3000=1"),  # ideographic space
        (" x", " x"),
        # A trigger after a list separator inside the value
        ("a,=1", "a,'=1"),
        ("a;=1", "a;'=1"),
        ("a, =1", "a, '=1"),
        ("a;  +1", "a;  '+1"),
        ("Rolls, -5 off", "Rolls, '-5 off"),
        ("a,\uff1d1", "a,'\uff1d1"),
        ("a, b", "a, b"),
        ("a,b-c", "a,b-c"),
        # Both at once, and several separators
        ("=a, @b; -c", "'=a, '@b; '-c"),
    ],
)
def test_guard_formula(value: str, guarded: str) -> None:
    assert guard_formula(value) == guarded


def test_a_guarded_value_can_exceed_the_import_length_limit() -> None:
    description = ", -1" * 50  # 200 characters
    assert len(description) == 200
    assert len(guard_formula(description)) == 250


def test_the_formula_guard_covers_description_and_category_only() -> None:
    options = CsvOptions(formula_guard=True)
    text = _text([_row(category="=Food", description="-5 refund")], options)
    assert ",'=Food,'-5 refund," in text
    assert "2026-09-02,expense,50.00,EUR," in text
