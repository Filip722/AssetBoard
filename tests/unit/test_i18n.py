from __future__ import annotations

from pathlib import Path

from babel.messages.pofile import read_po


def test_polish_catalog_has_no_empty_or_fuzzy_translations() -> None:
    catalog_path = Path(__file__).parents[2] / "src/assetboard/locales/pl/LC_MESSAGES/messages.po"
    with catalog_path.open("rb") as catalog_file:
        catalog = read_po(catalog_file)

    translated_messages = [message for message in catalog if message.id]
    assert translated_messages
    for message in translated_messages:
        assert "fuzzy" not in message.flags
        translations = message.string
        if isinstance(translations, str):
            translations = (translations,)
        assert translations is not None
        assert all(translation.strip() for translation in translations)


def test_month_names_keep_cldr_case_and_capitalise_only_headings() -> None:
    from assetboard.domain.months import YearMonth
    from assetboard.i18n.dates import format_month, format_month_heading

    october = YearMonth(2026, 10)
    assert format_month(october, "pl") == "październik 2026"
    assert format_month_heading(october, "pl") == "Październik 2026"
    assert format_month(october, "en") == format_month_heading(october, "en") == "October 2026"
    assert format_month(october, "xx-unknown") == "October 2026"  # unknown locale: English
