"""Pure category rules (F2.1): names, name keys, colours, reordering and the seed set."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.categories import (
    MAX_CATEGORY_NAME_LENGTH,
    MAX_NAME_KEY_LENGTH,
    SEED,
    InvalidCategoryName,
    InvalidColor,
    Kind,
    Treatment,
    clean_color,
    clean_name,
    move,
    name_key,
)


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Food", "Food"),
        ("  Eating   out ", "Eating out"),
        ("Żywność", "Żywność"),
        ("x" * MAX_CATEGORY_NAME_LENGTH, "x" * MAX_CATEGORY_NAME_LENGTH),
    ],
)
def test_clean_name(raw: str, clean: str) -> None:
    assert clean_name(raw) == clean


@pytest.mark.parametrize(
    "raw", ["", "   ", "x" * (MAX_CATEGORY_NAME_LENGTH + 1), "Bad‮name", "tab\x00"]
)
def test_clean_name_rejects(raw: str) -> None:
    with pytest.raises(InvalidCategoryName):
        clean_name(raw)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Food", "FOOD"),
        ("Żywność", "żYWNOŚĆ"),  # beyond ASCII, unlike SQLite's lower()
        ("Straße", "STRASSE"),
        ("Eating  out", "eating out"),
        ("ﬁsh", "fish"),  # NFKC folds the ligature
    ],
)
def test_name_key_matches_case_variants(a: str, b: str) -> None:
    assert name_key(a) == name_key(b)


def test_name_key_tells_different_names_apart() -> None:
    assert name_key("Food") != name_key("Foods")


@given(st.text(min_size=1, max_size=MAX_CATEGORY_NAME_LENGTH))
def test_name_key_is_idempotent_case_blind_and_fits_its_column(text: str) -> None:
    key = name_key(text)
    assert name_key(key) == key
    assert name_key(text.casefold()) == key
    assert name_key(f"  {text}  ") == key
    assert len(key) <= MAX_NAME_KEY_LENGTH


@pytest.mark.parametrize(("raw", "clean"), [("#16A34A", "#16a34a"), (" #000000 ", "#000000")])
def test_clean_color(raw: str, clean: str) -> None:
    assert clean_color(raw) == clean


@pytest.mark.parametrize("raw", ["", "red", "#fff", "#1234567", "16a34a", "#gg0000", "url(x)"])
def test_clean_color_rejects(raw: str) -> None:
    with pytest.raises(InvalidColor):
        clean_color(raw)


@pytest.mark.parametrize(
    ("item", "direction", "result"),
    [
        ("b", "up", ["b", "a", "c"]),
        ("b", "down", ["a", "c", "b"]),
        ("a", "up", ["a", "b", "c"]),
        ("c", "down", ["a", "b", "c"]),
    ],
)
def test_move(item: str, direction: str, result: list[str]) -> None:
    assert move(["a", "b", "c"], item, direction) == result  # type: ignore[arg-type]


@given(
    st.lists(st.integers(), min_size=1, max_size=20, unique=True),
    st.data(),
    st.sampled_from(["up", "down"]),
)
def test_move_is_a_permutation_changing_at_most_two_places(
    items: list[int], data: st.DataObject, direction: str
) -> None:
    item = data.draw(st.sampled_from(items))
    moved = move(items, item, direction)  # type: ignore[arg-type]
    assert sorted(moved) == sorted(items)
    assert sum(1 for before, after in zip(items, moved, strict=True) if before != after) in (0, 2)
    back = move(moved, item, "down" if direction == "up" else "up")
    assert back == items or moved == items


def test_seed_set_matches_the_spec() -> None:
    def names(kind: Kind, treatment: Treatment) -> list[str]:
        return [s.name for s in SEED if (s.kind, s.treatment) == (kind, treatment)]

    assert names(Kind.EXPENSE, Treatment.REGULAR) == [
        "Food", "Eating out", "Gifts", "Health", "Housing", "Transport", "Cosmetics", "Hobbies",
        "Utilities", "Travel", "Entertainment", "Education", "Taxes", "Clothes", "Other",
    ]  # fmt: skip
    assert names(Kind.EXPENSE, Treatment.SAVINGS) == ["Savings", "Investments"]
    assert names(Kind.INCOME, Treatment.REGULAR) == [
        "Salary", "Bonus", "Interest", "Sale of items", "Other",
    ]  # fmt: skip
    assert names(Kind.INCOME, Treatment.SAVINGS) == ["Savings withdrawal"]
    assert not any(s.treatment is Treatment.TRANSFER for s in SEED)
    for seed in SEED:
        assert clean_color(seed.color) == seed.color
        assert clean_name(seed.name) == seed.name


def test_a_name_whose_key_would_not_fit_its_column_is_refused() -> None:
    # U+FDFA is one character that NFKC expands to 18: 80 of them would make a 1440-character key.
    with pytest.raises(InvalidCategoryName):
        clean_name("ﷺ" * MAX_CATEGORY_NAME_LENGTH)
    fits = "ﷺ" * (MAX_NAME_KEY_LENGTH // 18)
    assert len(name_key(clean_name(fits))) <= MAX_NAME_KEY_LENGTH


@pytest.mark.parametrize("name", ["👨‍👩‍👧", "❤️ Charity", "Hobbies 🏳️‍🌈"])
def test_emoji_joiners_are_allowed(name: str) -> None:
    assert clean_name(name) == name


@pytest.mark.parametrize("name", ["a​b", "a⁠b", "‮evil", "a‌b"])
def test_other_format_characters_are_still_refused(name: str) -> None:
    with pytest.raises(InvalidCategoryName):
        clean_name(name)
