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
