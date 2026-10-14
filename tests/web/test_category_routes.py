"""Categories page over HTTP (F2.1): plain forms, HTMX fragments, roles and CSP-safe markup."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from assetboard.db.models.ledger import Category
from assetboard.services import categories as category_service
from assetboard.services.categories import CategoryUsage
from tests.conftest import OwnerApp

NO_PERMISSION = "You do not have permission to view this page."
FORM = {"name": "X", "kind": "expense", "treatment": "regular", "color": "#000000"}


def _htmx(owner: OwnerApp, path: str, data: dict[str, str] | None = None) -> Any:
    headers = {"HX-Request": "true", "X-CSRF-Token": owner.token("/categories")}
    return owner.client.post(path, data=data or {}, headers=headers, follow_redirects=False)


def _create(owner: OwnerApp, name: str, kind: str = "expense", **values: str) -> UUID:
    form = {"name": name, "kind": kind, "treatment": "regular", "color": "#16a34a"} | values
    response = owner.post("/categories", form, token_from="/categories")
    assert response.status_code == 303, response.text
    with owner.db() as session:
        return session.scalars(
            select(Category.id).where(Category.name == name, Category.kind == kind)
        ).one()


def _names(html: str, kind: str) -> list[str]:
    match = re.search(rf'data-section="{kind}".*?</section>', html, re.DOTALL)
    assert match is not None
    return re.findall(r"data-category-name>([^<]+)<", match.group(0))


@pytest.fixture
def in_use(monkeypatch: pytest.MonkeyPatch) -> set[UUID]:
    """Category ids reported as referenced (stands in for F2.2's transactions)."""
    used: set[UUID] = set()
    monkeypatch.setattr(
        category_service,
        "USAGE_SOURCES",
        [lambda _s, _c: {i: CategoryUsage(references=2, active_transactions=2) for i in used}],
    )
    return used


def test_the_nav_links_to_the_categories_page(owner: OwnerApp) -> None:
    assert "data-categories-link" in owner.client.get("/").text


def test_an_empty_workspace_offers_the_seed_set_to_editors(owner: OwnerApp) -> None:
    page = owner.client.get("/categories").text
    assert "data-seed-offer" in page and "data-seed" in page
    response = owner.post("/categories/seed", token_from="/categories")
    assert (response.status_code, response.headers["location"]) == (
        303,
        "/categories?notice=seeded",
    )
    page = owner.client.get("/categories?notice=seeded").text
    assert "Suggested categories added." in page
    assert "data-seed-offer" not in page
    assert _names(page, "expense")[:2] == ["Food", "Eating out"]
    assert _names(page, "income")[-1] == "Savings withdrawal"


def test_the_seed_set_is_named_in_the_users_language(owner: OwnerApp) -> None:
    owner.client.headers["Accept-Language"] = "pl"
    owner.post("/categories/seed", token_from="/categories")
    page = owner.client.get("/categories").text
    assert _names(page, "expense")[0] == "Jedzenie"
    assert "Kategorie wydatków" in page


def test_create_shows_the_category_in_order(owner: OwnerApp) -> None:
    _create(owner, "Rent")
    _create(owner, "Food")
    _create(owner, "Salary", "income")
    page = owner.client.get("/categories").text
    assert _names(page, "expense") == ["Rent", "Food"]
    assert _names(page, "income") == ["Salary"]


def test_invalid_and_duplicate_names_re_render_with_an_error(owner: OwnerApp) -> None:
    _create(owner, "Food")
    form = {"name": "FOOD", "kind": "expense", "treatment": "regular", "color": "#16a34a"}
    response = owner.post("/categories", form, token_from="/categories")
    assert response.status_code == 400
    assert "Another active category of this kind already has this name." in response.text
    assert 'value="FOOD"' in response.text
    bad = form | {"name": "Ok", "color": "javascript:alert(1)"}
    response = owner.post("/categories", bad, token_from="/categories")
    assert response.status_code == 400 and "Choose a colour." in response.text


def test_htmx_create_returns_the_body_fragment(owner: OwnerApp) -> None:
    form = {"name": "Food", "kind": "expense", "treatment": "regular", "color": "#16a34a"}
    response = _htmx(owner, "/categories", form)
    assert response.status_code == 200
    assert response.text.lstrip().startswith('<div id="categories-body"')
    assert "<html" not in response.text
    assert _names(response.text, "expense") == ["Food"]
    bad = _htmx(owner, "/categories", form | {"name": " "})
    assert bad.status_code == 400 and 'id="categories-body"' in bad.text


def test_inline_edit_fragment_save_and_cancel(owner: OwnerApp) -> None:
    food = _create(owner, "Food")
    headers = {"HX-Request": "true"}
    edit = owner.client.get(f"/categories/{food}/edit", headers=headers)
    assert edit.status_code == 200
    assert f'id="category-{food}"' in edit.text and "data-editing" in edit.text
    assert "<html" not in edit.text
    cancel = owner.client.get(f"/categories/{food}/row", headers=headers)
    assert "data-editing" not in cancel.text and "data-category-name>Food<" in cancel.text

    form = {"name": "Groceries", "kind": "expense", "treatment": "savings", "color": "#0ea5e9"}
    saved = _htmx(owner, f"/categories/{food}", form)
    assert saved.status_code == 200
    assert _names(saved.text, "expense") == ["Groceries"]
    assert 'data-treatment="savings"' in saved.text
    assert 'fill="#0ea5e9"' in saved.text


def test_inline_edit_without_javascript_uses_the_page(owner: OwnerApp) -> None:
    food = _create(owner, "Food")
    response = owner.client.get(f"/categories/{food}/edit", follow_redirects=False)
    assert response.headers["location"] == f"/categories?edit={food}"
    page = owner.client.get(f"/categories?edit={food}").text
    assert "data-editing" in page
    form = {"name": "Groceries", "kind": "expense", "treatment": "regular", "color": "#16a34a"}
    response = owner.post(f"/categories/{food}", form, token_from="/categories")
    assert response.headers["location"] == "/categories?notice=saved"


def test_an_edit_error_keeps_the_row_in_edit_mode(owner: OwnerApp) -> None:
    _create(owner, "Food")
    rent = _create(owner, "Rent")
    form = {"name": "food", "kind": "expense", "treatment": "regular", "color": "#16a34a"}
    response = _htmx(owner, f"/categories/{rent}", form)
    assert response.status_code == 400
    assert "data-editing" in response.text and 'value="food"' in response.text


def test_kind_of_a_category_in_use_is_locked(owner: OwnerApp, in_use: set[UUID]) -> None:
    food = _create(owner, "Food")
    in_use.add(food)
    edit = owner.client.get(f"/categories/{food}/edit", headers={"HX-Request": "true"}).text
    assert "data-kind-locked" in edit
    assert "A treatment change applies to 2 transactions." in edit
    form = {"name": "Food", "kind": "income", "treatment": "regular", "color": "#16a34a"}
    response = _htmx(owner, f"/categories/{food}", form)
    assert response.status_code == 400
    assert "The kind cannot change: this category is in use." in response.text


def test_move_reorders_and_returns_the_fragment(owner: OwnerApp) -> None:
    _create(owner, "A")
    b = _create(owner, "B")
    response = _htmx(owner, f"/categories/{b}/move", {"direction": "up"})
    assert _names(response.text, "expense") == ["B", "A"]
    response = owner.post(f"/categories/{b}/move", {"direction": "down"}, token_from="/categories")
    assert response.status_code == 303
    assert _names(owner.client.get("/categories").text, "expense") == ["A", "B"]


def test_archive_restore_and_delete(owner: OwnerApp) -> None:
    food = _create(owner, "Food")
    archived = _htmx(owner, f"/categories/{food}/archive")
    assert "Category archived." in archived.text
    assert _names(archived.text, "expense") == []
    assert "data-archived" in archived.text and "data-restore" in archived.text
    restored = _htmx(owner, f"/categories/{food}/restore")
    assert _names(restored.text, "expense") == ["Food"]
    deleted = owner.post(f"/categories/{food}/delete", token_from="/categories")
    assert deleted.headers["location"] == "/categories?notice=deleted"
    with owner.db() as session:
        assert session.get(Category, food) is None


def test_deleting_a_category_in_use_archives_it(owner: OwnerApp, in_use: set[UUID]) -> None:
    food = _create(owner, "Food")
    in_use.add(food)
    page = owner.client.get("/categories").text
    row = re.search(rf'id="category-{food}".*?</li>', page, re.DOTALL)
    assert row is not None and "data-delete" not in row.group(0)  # Delete only when unused
    assert "data-archive" in row.group(0)
    response = _htmx(owner, f"/categories/{food}/delete")
    assert "Archived because it is in use." in response.text
    with owner.db() as session:
        stored = session.get(Category, food)
        assert stored is not None and stored.archived_at is not None


def test_restoring_onto_an_active_name_shows_an_error(owner: OwnerApp) -> None:
    food = _create(owner, "Food")
    owner.post(f"/categories/{food}/archive", token_from="/categories")
    _create(owner, "food")
    response = _htmx(owner, f"/categories/{food}/restore")
    assert response.status_code == 409
    assert "Another active category of this kind already has this name." in response.text


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/categories", FORM),
        ("/categories/{id}", FORM),
        ("/categories/{id}/move", {"direction": "up"}),
        ("/categories/{id}/archive", {}),
        ("/categories/{id}/restore", {}),
        ("/categories/{id}/delete", {}),
        ("/categories/seed", {}),
    ],
)  # fmt: skip
def test_viewers_see_no_controls_and_cannot_write(
    owner: OwnerApp, path: str, data: dict[str, str]
) -> None:
    food = _create(owner, "Food")
    owner.set_admin_role("viewer")
    page = owner.client.get("/categories").text
    assert _names(page, "expense") == ["Food"]
    for marker in ("data-create", "data-edit", "data-archive", "data-delete", "data-move-up"):
        assert marker not in page
    response = owner.post(path.format(id=food), data, token_from="/categories")
    assert response.status_code == 403
    assert NO_PERMISSION in response.text
    assert "data-editing" not in owner.client.get(f"/categories?edit={food}").text
    with owner.db() as session:
        stored = session.get(Category, food)
        assert stored is not None and (stored.name, stored.archived_at) == ("Food", None)


def test_another_workspaces_category_is_404(owner: OwnerApp) -> None:
    food = _create(owner, "Food")
    other = owner.add_workspace("Other")
    owner.join(owner.admin_id, other, "owner")
    owner.post("/workspace/switch", {"workspace_id": str(other)})
    for path in (f"/categories/{food}/archive", f"/categories/{food}/delete"):
        assert owner.post(path, token_from="/categories").status_code == 404
    edit = owner.client.get(f"/categories/{food}/edit", headers={"HX-Request": "true"})
    assert edit.status_code == 404
    assert owner.client.get(f"/categories?edit={uuid4()}").status_code == 404
    assert owner.client.get("/categories?edit=nope").status_code == 404


def test_writes_need_a_csrf_token(owner: OwnerApp) -> None:
    form = {"name": "Food", "kind": "expense", "treatment": "regular", "color": "#16a34a"}
    response = owner.client.post("/categories", data=form, follow_redirects=False)
    assert response.status_code == 403
    assert "data-seed-offer" in owner.client.get("/categories").text


def test_markup_is_csp_safe_and_escaped(owner: OwnerApp) -> None:
    _create(owner, "<b>Fun</b> & games")
    page = owner.client.get("/categories").text
    assert "<b>Fun</b>" not in page and "&lt;b&gt;Fun&lt;/b&gt; &amp; games" in page
    assert " style=" not in page  # style-src 'self': swatches use SVG fill, not inline styles
    assert re.search(r"<script(?![^>]*\ssrc=)", page) is None
    assert "x-data" not in page and "@click" not in page
    assert '<circle cx="7" cy="7" r="7" fill="#16a34a"/>' in page


def test_category_events_appear_in_the_audit_log_translated(owner: OwnerApp) -> None:
    food = _create(owner, "Food, drinks")
    form = {"name": "Food, drinks", "kind": "expense", "treatment": "savings", "color": "#16a34a"}
    owner.post(f"/categories/{food}", form, token_from="/categories")
    page = owner.client.get("/audit?scope=workspace&group=categories").text
    assert "Category added" in page and "Category changed" in page
    assert "Food, drinks" in page  # free text is not split like a list of codes
    assert "Previous treatment: regular" in page
    owner.client.headers["Accept-Language"] = "pl"
    page = owner.client.get("/audit?scope=workspace&group=categories").text
    assert "Dodano kategorię" in page and "Poprzednie traktowanie: zwykłe" in page


def test_the_page_never_opens_an_edit_form_for_an_archived_category(owner: OwnerApp) -> None:
    food = _create(owner, "Food")
    owner.post(f"/categories/{food}/archive", token_from="/categories")
    page = owner.client.get(f"/categories?edit={food}")
    assert page.status_code == 200
    assert "data-editing" not in page.text and "data-restore" in page.text


def test_an_unknown_move_direction_is_400(owner: OwnerApp) -> None:
    _create(owner, "A")
    b = _create(owner, "B")
    response = owner.post(f"/categories/{b}/move", {"direction": "left"}, token_from="/categories")
    assert response.status_code == 400
    assert _names(owner.client.get("/categories").text, "expense") == ["A", "B"]
