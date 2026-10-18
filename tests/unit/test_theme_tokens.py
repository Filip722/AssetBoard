"""Colour tokens: WCAG AA contrast in both themes, and templates use only tokens."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
INPUT_CSS = ROOT / "src/assetboard/static/css/input.css"
APP_CSS = ROOT / "src/assetboard/static/css/app.css"
TEMPLATES = ROOT / "src/assetboard/web/templates"

TEXT_AA = 4.5
NON_TEXT = 3.0  # WCAG 1.4.11: borders that identify a form control

# Text set in a component class (input.css), where no template class attribute shows it.
CSS_TEXT_PAIRS = {
    ("ink", "canvas"),  # body
    ("muted", "surface"),  # .nav-link in the sidebar
    ("ink", "subtle"),  # .nav-link:hover
    ("primary-ink", "primary-soft"),  # .nav-link[aria-current]
    ("ink", "field"),  # .form-control, .form-select
    ("muted", "field"),  # placeholders, disabled controls
    ("muted", "subtle"),  # disabled controls
    ("inverse-link", "inverse"),  # the toast's undo button sits on the toast's background
}
# Text with no background of its own sits on the page or on a card (.panel).
INHERITED_BACKGROUNDS = ("canvas", "surface")
# Text meant only for its own background: never checked against an inherited one.
OWN_BACKGROUND_ONLY = re.compile(r"^(on-|inverse-link$)")

NON_TEXT_PAIRS = [
    ("line-strong", "field"),
    ("line-strong", "surface"),
    ("primary-ink", "canvas"),  # focus outline
    ("primary-ink", "surface"),
]


def _theme(selector: str) -> dict[str, tuple[int, int, int]]:
    css = INPUT_CSS.read_text(encoding="utf-8")
    block = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
    assert block is not None, selector
    return {
        name: (int(r), int(g), int(b))
        for name, r, g, b in re.findall(
            r"--color-([a-z-]+):\s*(\d+)\s+(\d+)\s+(\d+);", block.group(1)
        )
    }


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


THEMES = {"light": ":root", "dark": ":root.dark"}


def test_contrast_formula_matches_known_values() -> None:
    assert contrast((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0)
    # White on #03a9f4 is the shade the owner suggested; it fails AA, so buttons use #0277bd.
    assert contrast((255, 255, 255), (3, 169, 244)) == pytest.approx(2.63, abs=0.01)


def test_both_themes_define_the_same_tokens() -> None:
    light, dark = _theme(":root"), _theme(":root.dark")
    assert light.keys() == dark.keys()
    assert len(light) >= 25


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_text_pairs_meet_wcag_aa(theme: str) -> None:
    tokens = _theme(THEMES[theme])
    pairs = template_text_pairs() | CSS_TEXT_PAIRS
    assert ("warning-strong", "surface") in pairs  # derived, not listed by hand
    failures = [
        f"{fg} on {bg}: {contrast(tokens[fg], tokens[bg]):.2f}"
        for fg, bg in sorted(pairs)
        if contrast(tokens[fg], tokens[bg]) < TEXT_AA
    ]
    assert not failures, f"{theme}: {failures}"


@pytest.mark.parametrize("theme", sorted(THEMES))
@pytest.mark.parametrize(("fg", "bg"), NON_TEXT_PAIRS)
def test_control_borders_and_focus_meet_non_text_contrast(theme: str, fg: str, bg: str) -> None:
    tokens = _theme(THEMES[theme])
    ratio = contrast(tokens[fg], tokens[bg])
    assert ratio >= NON_TEXT, f"{theme}: {fg} on {bg} is {ratio:.2f}"


def test_palette_matches_the_approved_values() -> None:
    light, dark = _theme(":root"), _theme(":root.dark")
    assert light["canvas"] == (0xFA, 0xFA, 0xFA) and dark["canvas"] == (0x11, 0x11, 0x11)
    assert light["surface"] == (0xFF, 0xFF, 0xFF) and dark["surface"] == (0x1C, 0x1C, 0x1C)
    assert light["ink"] == (0x21, 0x21, 0x21) and dark["ink"] == (0xE1, 0xE1, 0xE1)
    assert light["primary"] == dark["primary"] == (0x02, 0x77, 0xBD)
    assert light["accent"] == dark["accent"] == (0xFF, 0x98, 0x00)


def test_built_css_contains_both_themes() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    assert ":root.dark{" in css and "--color-canvas:17 17 17" in css
    assert "--color-canvas:250 250 250" in css


# Utilities that take a colour. Each must name a theme token, or be one of the non-colour
# utilities sharing its prefix (sizes, sides, styles).
COLOUR_PREFIXES = (
    "text|bg|border|ring|outline|divide|fill|stroke|placeholder|accent|caret|decoration|shadow|"
    "from|via|to"
)
UTILITY = re.compile(
    rf"^(?P<variants>(?:[a-z0-9-]+:)*)!?(?P<prefix>{COLOUR_PREFIXES})-(?P<value>.+)$"
)
NON_COLOUR = {
    "text": (
        r"(xs|sm|base|lg|[0-9]?xl|left|center|right|justify|start|end|ellipsis|clip|wrap|"
        r"nowrap|balance|pretty)"
    ),
    "bg": (
        r"(no-repeat|repeat(-[xy])?|cover|contain|auto|center|fixed|local|scroll|none|"
        r"(left|right|top|bottom)(-(left|right|top|bottom))?|clip-\w+|origin-\w+)"
    ),
    "border": (
        r"([0-9]+|[xytrblse](-[0-9]+)?|solid|dashed|dotted|double|hidden|none|collapse|"
        r"separate)"
    ),
    "ring": r"([0-9]+|inset|offset-[0-9]+)",
    "outline": r"(none|[0-9]+|offset-[0-9]+|dashed|dotted|double)",
    "divide": r"([xy](-[0-9]+)?|[xy]-reverse|solid|dashed|dotted|double|none)",
    "shadow": r"(sm|md|lg|xl|2xl|inner|none|card)",
    "decoration": r"([0-9]+|solid|double|dotted|dashed|wavy|auto|from-font|clone|slice)",
    "from": r"[0-9]+%",
    "via": r"[0-9]+%",
    "to": r"[0-9]+%",
    "fill": r"none",
    "stroke": r"(none|[0-9]+)",
}
# Explicitly allowed exceptions: the dialog backdrop dims whatever is behind it.
ALLOWED = {"backdrop:bg-black/50"}


def _class_lists(text: str) -> list[list[str]]:
    """Class attributes, and class strings kept in Jinja variables (``{% set x = "..." %}``)."""
    found = re.findall(r'class="([^"]*)"', text)
    found += re.findall(r'\{%-?\s*set\s+\w+\s*=\s*"([^"]*)"\s*-?%\}', text)
    return [value.split() for value in found]


def _tokens() -> set[str]:
    return set(_theme(":root"))


def _is_token(value: str, tokens: set[str]) -> bool:
    return value.split("/", 1)[0] in tokens


def template_text_pairs() -> set[tuple[str, str]]:
    """(text token, background token) pairs the templates render.

    Text and background named in the same class attribute (any state: hover and the like are
    paired too, which can only add pairs), and every text token on the page and card
    backgrounds it inherits.
    """
    tokens = _tokens()
    pairs: set[tuple[str, str]] = set()
    for path in TEMPLATES.rglob("*.html"):
        for classes in _class_lists(path.read_text(encoding="utf-8")):
            texts, backgrounds = set(), set()
            for item in classes:
                match = UTILITY.match(item)
                if match is None or "/" in match["value"]:
                    continue
                if match["prefix"] == "text" and match["value"] in tokens:
                    texts.add(match["value"])
                elif match["prefix"] == "bg" and match["value"] in tokens:
                    backgrounds.add(match["value"])
            pairs |= {(fg, bg) for fg in texts for bg in backgrounds}
            pairs |= {
                (fg, bg)
                for fg in texts
                if not OWN_BACKGROUND_ONLY.match(fg)
                for bg in INHERITED_BACKGROUNDS
            }
    return pairs


@pytest.mark.parametrize("path", sorted(TEMPLATES.rglob("*.html")), ids=lambda p: p.name)
def test_templates_use_only_colour_tokens(path: Path) -> None:
    tokens = _tokens()
    problems = []
    for classes in _class_lists(path.read_text(encoding="utf-8")):
        for item in classes:
            match = UTILITY.match(item)
            if match is None or item in ALLOWED:
                continue
            value, prefix = match["value"], match["prefix"]
            if "[" in value:
                problems.append(f"{item}: arbitrary value")
            elif "dark:" in match["variants"]:
                problems.append(f"{item}: tokens switch with the theme")
            elif prefix == "text" and value == "accent":
                # Orange is a background colour only; as text it fails AA on light themes.
                problems.append(f"{item}: accent is not a text colour")
            elif not _is_token(value, tokens) and not re.fullmatch(
                NON_COLOUR.get(prefix, r"(?!)"), value
            ):
                problems.append(f"{item}: use a theme token")
    assert not problems, f"{path.name}: {problems}"


def test_the_colour_guard_catches_what_it_should() -> None:
    tokens = _tokens()

    def bad(item: str) -> bool:
        match = UTILITY.match(item)
        assert match is not None, item
        value = match["value"]
        return (
            "[" in value
            or "dark:" in match["variants"]
            or not (_is_token(value, tokens) or re.fullmatch(NON_COLOUR[match["prefix"]], value))
        )

    for item in (
        "text-slate-500",
        "bg-white",
        "hover:bg-emerald-50",
        "dark:bg-surface",
        "bg-[#123456]",
        "text-[#fff]",
        "fill-red-500",
        "shadow-black",
        "from-sky-400",
        "decoration-amber-300",
        "divide-gray-100",
        "ring-offset-white",
        "border-x-blue-500",
    ):
        assert bad(item), item
    for item in (
        "text-sm",
        "bg-surface",
        "hover:bg-subtle",
        "border-b-0",
        "ring-primary/40",
        "divide-y",
        "shadow-card",
        "focus:ring-2",
        "text-on-primary",
    ):
        assert not bad(item), item


def test_every_select_uses_the_shared_select_class() -> None:
    for path in TEMPLATES.rglob("*.html"):
        for tag in re.findall(r"<select\b[^>]*>", path.read_text(encoding="utf-8"), re.S):
            classes = re.search(r'class="([^"]*)"', tag)
            assert classes is not None and "form-select" in classes.group(1).split(), path
            # Horizontal padding on the select itself would cover the chevron's space.
            assert not re.search(r"(?<![\w-])(px|pr)-", classes.group(1)), path
