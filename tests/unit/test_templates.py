from __future__ import annotations

import hashlib
import re
from pathlib import Path

from assetboard.domain.money import Money
from assetboard.web.rendering import templates_for


def test_form_field_renders_validation_errors_safely() -> None:
    templates = templates_for("en")
    rendered = templates.env.from_string(
        '{% from "macros/components.html" import field %}'
        '{{ field("amount", "Amount", error="<invalid>") }}'
    ).render()

    assert 'aria-invalid="true"' in rendered
    assert 'aria-describedby="amount-error"' in rendered
    assert "&lt;invalid&gt;" in rendered


def test_undo_toast_uses_htmx_out_of_band_swap() -> None:
    templates = templates_for("en")
    rendered = templates.env.from_string(
        '{% from "macros/components.html" import toast %}'
        '{{ toast("Deleted", "/undo", "Undo") }}'
        '{{ toast("Deleted again", "/undo", "Undo") }}'
    ).render()

    toast_ids = re.findall(r'id="(toast-[a-f0-9]+)"', rendered)
    assert len(toast_ids) == 2
    assert len(set(toast_ids)) == 2
    assert 'hx-swap-oob="beforeend:#toast-container"' in rendered
    assert rendered.count('data-toast role="status"') == 2
    assert 'hx-post="/undo"' in rendered
    assert "Undo" in rendered
    # Undo toasts are strict (no pause on hover or focus) and are replaced by the undo response.
    assert rendered.count('data-toast-strict data-toast-seconds="10"') == 2
    assert rendered.count('hx-target="closest [data-toast]" hx-swap="outerHTML"') == 2
    assert rendered.count("data-toast-countdown>10</span>") == 2


def test_a_toast_without_undo_is_not_strict() -> None:
    templates = templates_for("en")
    rendered = templates.env.from_string(
        '{% from "macros/components.html" import toast %}{{ toast("Saved", None, "Undo") }}'
    ).render()

    assert "data-toast-strict" not in rendered and "data-toast-countdown" not in rendered


def test_vendored_javascript_matches_documented_hashes() -> None:
    repository = Path(__file__).parents[2]
    vendor_directory = repository / "src/assetboard/static/vendor"
    manifest = (vendor_directory / "README.md").read_text(encoding="utf-8")
    assets = re.findall(
        r"^\| `([^`]+\.js)` \| [^|]+ \| [^|]+ \| `([0-9a-f]{64})` \|",
        manifest,
        re.M,
    )

    assert len(assets) == 3
    for asset, expected_hash in assets:
        actual_hash = hashlib.sha256((vendor_directory / asset).read_bytes()).hexdigest()
        assert actual_hash == expected_hash


def test_each_vendored_javascript_has_a_documented_license() -> None:
    repository = Path(__file__).parents[2]
    vendor_directory = repository / "src/assetboard/static/vendor"
    manifest = (vendor_directory / "README.md").read_text(encoding="utf-8")
    javascript_files = sorted(vendor_directory.glob("*.js"))

    assert javascript_files
    for javascript_file in javascript_files:
        asset_rows = [
            line
            for line in manifest.splitlines()
            if line.startswith("|") and f"`{javascript_file.name}`" in line
        ]
        assert len(asset_rows) == 1, f"README must list {javascript_file.name} once"
        columns = [column.strip() for column in asset_rows[0].strip("|").split("|")]
        assert len(columns) >= 5
        assert columns[2], f"README must name a license for {javascript_file.name}"
        license_file_name = columns[4].strip("`")
        assert license_file_name.endswith("-LICENSE.txt")
        assert (vendor_directory / license_file_name).is_file()


def test_built_css_uses_class_based_dark_mode() -> None:
    css_path = Path(__file__).parents[2] / "src/assetboard/static/css/app.css"
    css = css_path.read_text(encoding="utf-8")

    assert ".dark" in css
    assert "prefers-color-scheme:dark" not in css


def test_trans_block_escapes_interpolated_values() -> None:
    templates = templates_for("en")
    rendered = templates.env.from_string(
        '{% trans name="<script>alert(1)</script>" %}Hello {{ name }}{% endtrans %}'
    ).render()

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "<script>" not in rendered


def test_money_macro_formats_money_for_active_locale() -> None:
    templates = templates_for("pl")
    rendered = templates.env.from_string(
        '{% from "macros/components.html" import money with context %}{{ money(value) }}'
    ).render(value=Money(123456, "EUR"), locale="pl")

    assert rendered == '<span class="tabular-nums">1 234,56 €</span>'
