"""Shared rules for user-entered text (names, descriptions). Pure: no I/O."""

from __future__ import annotations

import unicodedata

# Control, format, line/paragraph separators, private-use and unassigned characters.
_REJECTED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cn"})
# Format characters that emoji sequences need: ZERO WIDTH JOINER (U+200D) joins "👨‍👩‍👧".
# (VARIATION SELECTOR-16, U+FE0F, is a mark, not a format character, so it is allowed anyway.)
_ALLOWED_FORMAT_CHARACTERS = frozenset({"‍", "️"})


def has_rejected_characters(value: str) -> bool:
    """True if ``value`` contains control or invisible characters (emoji joiners are fine)."""
    return any(
        unicodedata.category(character) in _REJECTED_CATEGORIES
        and character not in _ALLOWED_FORMAT_CHARACTERS
        for character in value
    )


def collapse_whitespace(value: str) -> str:
    """Trim and collapse inner runs of whitespace to one space."""
    return " ".join(value.split())


def text_key(value: str) -> str:
    """The case-insensitive identity of a text: NFKC, Unicode casefold, whitespace collapsed.

    SQLite's ``lower()`` folds ASCII only, so case-insensitive matching uses a stored key.
    """
    folded = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", value).casefold())
    return collapse_whitespace(folded)
