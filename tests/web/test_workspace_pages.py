"""Workspace switcher, settings and members pages over HTTP (F1.2 part B1)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from assetboard.db.models.identity import Membership, UserSession, Workspace
from assetboard.db.models.ledger import BudgetLine, Category, Transaction
from assetboard.services.auth import session_id_hash
from assetboard.services.workspaces import WorkspaceError
from assetboard.web import workspace_routes
from assetboard.web.security import session_cookie_name
from tests.conftest import OwnerApp

LAST_OWNER = "A workspace needs at least one owner. Make another member an owner first."
NOT_FOUND = "The page you requested could not be found."
NO_PERMISSION = "You do not have permission to view this page."


def _switcher_options(html: str) -> list[str]:
    match = re.search(r"<select id=\"workspace-switcher\".*?</select>", html, re.DOTALL)
    assert match is not None, "no switcher"
    return re.findall(r">([^<]+)</option>", match.group(0))


# Switcher


def test_switcher_appears_only_with_several_workspaces(owner: OwnerApp) -> None:
    page = owner.client.get("/").text
    assert "data-workspace-switcher" not in page
    assert "data-current-workspace>Home<" in page

    second = owner.add_workspace("Second")
    owner.join(owner.admin_id, second, "viewer")
    page = owner.client.get("/").text
    assert "data-workspace-switcher" in page
    assert _switcher_options(page) == ["Home", "Second"]
    assert re.search(r'<option value="[^"]+" selected>Home</option>', page)


def test_switching_workspace_changes_the_context(owner: OwnerApp) -> None:
    second = owner.add_workspace("Second")
    owner.join(owner.admin_id, second, "viewer")
    response = owner.post("/workspace/switch", {"workspace_id": str(second)})
    assert (response.status_code, response.headers["location"]) == (303, "/")
    page = owner.client.get("/").text
    assert "data-current-workspace>Second<" in page
    assert re.search(r'<option value="[^"]+" selected>Second</option>', page)
    assert "data-owner-link" not in page  # viewer in Second


@pytest.mark.parametrize("target", ["foreign", "unknown", "malformed"])
def test_switch_refuses_foreign_workspace_with_404(owner: OwnerApp, target: str) -> None:
    foreign = owner.add_workspace("Foreign")
    value = {"foreign": str(foreign), "unknown": str(uuid4()), "malformed": "nope"}[target]
    response = owner.post("/workspace/switch", {"workspace_id": value})
    assert response.status_code == 404
    assert NOT_FOUND in response.text and "Foreign" not in response.text
    cookie = owner.client.cookies[session_cookie_name(owner.app.state.settings)]
    with owner.db() as session:
        row = session.get(UserSession, session_id_hash(cookie))
        assert row is not None and row.current_workspace_id is None


# Pages without a Context render base.html without the workspace parts


WORKSPACE_PARTS = (
    "data-workspace-switcher",
    "data-current-workspace",
    "data-owner-link",
    "data-recurring-link",
)


def test_home_shows_owner_links_only_to_owners(owner: OwnerApp) -> None:
    page = owner.client.get("/").text
    assert "data-owner-link" in page
    assert "data-recurring-link" in page
    owner.set_admin_role("viewer")
    assert "data-owner-link" not in owner.client.get("/").text


@pytest.mark.parametrize("page", ["account", "not_found", "no_workspace", "signed_out_login"])
def test_pages_without_a_context_render_without_workspace_parts(owner: OwnerApp, page: str) -> None:
    second = owner.add_workspace("Second")
    owner.join(owner.admin_id, second, "owner")  # the switcher would show on workspace pages
    client = owner.client
    if page == "account":
        response, status = client.get("/account/password"), 200
    elif page == "not_found":
        response, status = client.get("/no-such-page"), 404
    elif page == "no_workspace":
        with owner.db() as session, session.begin():
            session.execute(delete(Membership))
        response, status = client.get("/"), 403
    else:
        client.post("/logout", data={"csrf_token": owner.token("/account/password")})
        response, status = client.get("/login"), 200
    assert response.status_code == status
    for part in WORKSPACE_PARTS:
        assert part not in response.text
    assert "Personal workspace" not in response.text


# Settings


def test_owner_updates_workspace_settings(owner: OwnerApp) -> None:
    form = owner.client.get("/workspace/settings")
    assert form.status_code == 200 and 'value="Home"' in form.text
    response = owner.post(
        "/workspace/settings",
        {
            "name": "Family",
            "base_currency": "PLN",
            "country_code": "PL",
            "timezone": "Europe/Warsaw",
            "locale": "pl",
        },
        token_from="/workspace/settings",
    )
    assert (response.status_code, response.headers["location"]) == (
        303,
        "/workspace/settings?saved=1",
    )
    saved = owner.client.get(response.headers["location"]).text
    assert "Workspace settings saved." in saved
    assert "data-current-workspace>Family<" in saved
    with owner.db() as session:
        workspace = session.scalars(select(Workspace)).one()
        assert (workspace.name, workspace.base_currency, workspace.timezone) == (
            "Family",
            "PLN",
            "Europe/Warsaw",
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("base_currency", "XYZ", "Choose a supported currency."),
        ("country_code", "ZZ", "Choose a country from the list."),
        ("timezone", "Mars/Base", "Choose a time zone from the list."),
        ("locale", "xx", "Choose a supported language."),
    ],
)
def test_settings_option_errors_name_the_field(
    owner: OwnerApp, field: str, value: str, message: str
) -> None:
    form = {"name": "Home", "base_currency": "EUR", "timezone": "UTC", "locale": "en"}
    response = owner.post(
        "/workspace/settings", form | {field: value}, token_from="/workspace/settings"
    )
    assert response.status_code == 400
    assert f'aria-describedby="{field}-error"' in response.text
    assert message in response.text
    assert response.text.count('aria-invalid="true"') == 1


def test_settings_validation_errors_are_shown_on_the_field(owner: OwnerApp) -> None:
    response = owner.post(
        "/workspace/settings",
        {"name": "", "base_currency": "EUR", "timezone": "UTC", "locale": "en"},
        token_from="/workspace/settings",
    )
    assert response.status_code == 400
    assert 'aria-describedby="name-error"' in response.text
    assert "Enter a workspace name of 1 to 120 characters." in response.text


def _plant_transaction(owner: OwnerApp, *, deleted: bool) -> None:
    """A stored amount in the workspace's currency (soft-deleted or not)."""
    with owner.db() as session, session.begin():
        # "Food" is one of the categories every new workspace gets.
        category = session.scalars(
            select(Category).where(
                Category.workspace_id == owner.home_id, Category.name_key == "food"
            )
        ).one()
        session.add(
            Transaction(
                workspace_id=owner.home_id, direction="expense", date=date(2026, 10, 7),
                amount_minor=1250, currency="EUR", category_id=category.id, description="",
                description_key="", source="manual",
                deleted_at=datetime(2026, 10, 7, tzinfo=UTC) if deleted else None,
            )
        )  # fmt: skip


LOCKED_HELP = (
    "Locked: transactions (deleted ones included) or planned values or recurring items exist, "
    "and their amounts "
    "are stored in this currency."
)


def test_the_base_currency_select_is_free_without_amounts(owner: OwnerApp) -> None:
    page = owner.client.get("/workspace/settings").text
    assert "data-currency-locked" not in page
    assert '<select id="base_currency" name="base_currency"' in page


@pytest.mark.parametrize("deleted", [False, True])
def test_the_settings_page_shows_why_the_base_currency_is_locked(
    owner: OwnerApp, deleted: bool
) -> None:
    _plant_transaction(owner, deleted=deleted)
    page = owner.client.get("/workspace/settings").text
    assert "data-currency-locked" in page
    assert re.search(r'<select id="base_currency" disabled[^>]*base_currency-help', page)
    assert '<input type="hidden" name="base_currency" value="EUR">' in page
    assert LOCKED_HELP in page
    polish = owner.client.get("/workspace/settings", headers={"Accept-Language": "pl"}).text
    assert (
        "Zablokowana: istnieją transakcje (również usunięte), wartości planowane "
        "lub pozycje cykliczne, "
        "a ich kwoty są zapisane w tej walucie." in polish
    )


def test_a_planned_value_locks_the_base_currency_on_the_settings_page(owner: OwnerApp) -> None:
    with owner.db() as session, session.begin():
        category = Category(
            workspace_id=owner.home_id, name="Planned groceries", name_key="planned groceries",
            kind="expense", treatment="regular", color="#16a34a", sort_order=0,
        )  # fmt: skip
        session.add(category)
        session.flush()
        session.add(
            BudgetLine(
                workspace_id=owner.home_id, month=date(2026, 10, 1), category_id=category.id,
                planned_minor=55000, currency="EUR",
            )
        )  # fmt: skip
    page = owner.client.get("/workspace/settings").text
    assert "data-currency-locked" in page and LOCKED_HELP in page


def test_owners_turn_the_automatic_plan_copy_on_and_off(owner: OwnerApp) -> None:
    page = owner.client.get("/workspace/settings").text
    assert re.search(r'<input id="auto_copy_plan" name="auto_copy_plan" type="checkbox"', page)
    assert "checked" not in page.split('id="auto_copy_plan"')[1].split(">")[0]  # off by default
    form = {"name": "Home", "base_currency": "EUR", "timezone": "UTC", "locale": "en"}
    response = owner.post(
        "/workspace/settings", form | {"auto_copy_plan": "1"}, token_from="/workspace/settings"
    )
    assert response.status_code == 303
    with owner.db() as session:
        assert session.scalars(select(Workspace.auto_copy_plan)).one() is True
    page = owner.client.get("/workspace/settings").text
    assert " checked" in page.split('id="auto_copy_plan"')[1].split(">")[0]
    polish = owner.client.get("/workspace/settings", headers={"Accept-Language": "pl"}).text
    assert "Kopiuj plan z poprzedniego miesiąca przy pierwszym otwarciu miesiąca" in polish
    owner.post("/workspace/settings", form, token_from="/workspace/settings")  # unchecked box
    with owner.db() as session:
        assert session.scalars(select(Workspace.auto_copy_plan)).one() is False


def test_a_locked_base_currency_change_is_refused_on_the_field(owner: OwnerApp) -> None:
    _plant_transaction(owner, deleted=False)
    form = {"name": "Family", "base_currency": "PLN", "timezone": "UTC", "locale": "en"}
    response = owner.post("/workspace/settings", form, token_from="/workspace/settings")
    assert response.status_code == 400
    assert "The base currency cannot change: amounts are already stored in it." in response.text
    assert "base_currency-help base_currency-error" in response.text
    assert 'name="base_currency" value="EUR"' in response.text  # the stored one, not PLN
    with owner.db() as session:
        assert session.scalars(select(Workspace.base_currency)).one() == "EUR"
    response = owner.post(
        "/workspace/settings", form | {"base_currency": "EUR"}, token_from="/workspace/settings"
    )
    assert response.status_code == 303  # every other setting still changes
    owner.client.headers["Accept-Language"] = "pl"
    polish = owner.post("/workspace/settings", form, token_from="/workspace/settings")
    assert "Waluty bazowej nie można zmienić: są już w niej zapisane kwoty." in polish.text


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_settings_and_members_require_owner(owner: OwnerApp, role: str) -> None:
    token = owner.token()
    owner.set_admin_role(role)
    for method, path, data in (
        ("get", "/workspace/settings", None),
        ("post", "/workspace/settings", {"name": "X", "base_currency": "EUR"}),
        ("get", "/workspace/members", None),
        ("post", f"/workspace/members/{uuid4()}/role", {"role": "owner"}),
        ("post", f"/workspace/members/{uuid4()}/remove", {}),
    ):
        if method == "get":
            response = owner.client.get(path)
        else:
            response = owner.client.post(path, data={"csrf_token": token} | (data or {}))
        assert response.status_code == 403, path
        assert NO_PERMISSION in response.text
    assert owner.roles() == {"admin": role}


# Members


def test_members_page_lists_members(owner: OwnerApp) -> None:
    bea = owner.add_user("bea")
    owner.join(bea, owner.home_id, "editor")
    stranger = owner.add_user("stranger")
    owner.join(stranger, owner.add_workspace("Other"), "owner")
    page = owner.client.get("/workspace/members")
    assert page.status_code == 200
    assert "admin@example.com" in page.text and "bea@example.com" in page.text
    assert "stranger@example.com" not in page.text
    # No remove button for yourself.
    assert page.text.count("data-remove-member") == 1


def test_owner_changes_a_role_and_removes_a_member(owner: OwnerApp) -> None:
    bea = owner.add_user("bea")
    membership = owner.join(bea, owner.home_id, "viewer")
    response = owner.post(
        f"/workspace/members/{membership}/role", {"role": "editor"}, token_from="/workspace/members"
    )
    assert (response.status_code, response.headers["location"]) == (303, "/workspace/members")
    assert owner.roles()["bea"] == "editor"
    response = owner.post(
        f"/workspace/members/{membership}/remove", token_from="/workspace/members"
    )
    assert response.status_code == 303
    assert "bea" not in owner.roles()


def test_last_owner_is_refused_with_409(owner: OwnerApp) -> None:
    mine = owner.membership_id(owner.admin_id)
    response = owner.post(f"/workspace/members/{mine}/role", {"role": "viewer"})
    assert response.status_code == 409
    assert LAST_OWNER in response.text
    response = owner.post(f"/workspace/members/{mine}/remove")
    assert response.status_code == 409
    assert "You cannot remove yourself from the workspace." in response.text
    assert owner.roles() == {"admin": "owner"}


def test_role_change_of_foreign_membership_is_404(owner: OwnerApp) -> None:
    stranger = owner.add_user("stranger")
    other = owner.add_workspace("Other")
    foreign = owner.join(stranger, other, "viewer")
    for path, data in (
        (f"/workspace/members/{foreign}/role", {"role": "owner"}),
        (f"/workspace/members/{foreign}/remove", {}),
        ("/workspace/members/not-a-uuid/remove", {}),
    ):
        response = owner.post(path, data)
        assert response.status_code == 404
        assert "Other" not in response.text
    assert owner.roles(other) == {"stranger": "viewer"}


def test_invalid_role_is_400(owner: OwnerApp) -> None:
    mine = owner.membership_id(owner.admin_id)
    response = owner.post(f"/workspace/members/{mine}/role", {"role": "admin"})
    assert response.status_code == 400
    assert "Choose a role." in response.text


def test_removed_member_loses_access_on_next_request(owner: OwnerApp) -> None:
    bea = owner.add_user("bea")
    membership = owner.join(bea, owner.home_id, "editor")
    with TestClient(owner.app) as bea_client:
        assert owner.web.sign_in(bea_client, email="bea@example.com").status_code == 303
        assert bea_client.get("/").status_code == 200
        owner.post(f"/workspace/members/{membership}/remove", token_from="/workspace/members")
        page = bea_client.get("/")
        assert page.status_code == 403
        assert "not a member of any workspace" in page.text
        assert bea_client.get("/account/password").status_code == 200


@pytest.mark.parametrize("locale", ["en", "pl"])
def test_workspace_pages_render_in_english_and_polish(owner: OwnerApp, locale: str) -> None:
    headers = {"Accept-Language": locale}
    settings = owner.client.get("/workspace/settings", headers=headers).text
    members = owner.client.get("/workspace/members", headers=headers).text
    if locale == "pl":
        assert "Ustawienia obszaru roboczego" in settings
        assert "Członkowie" in members
    else:
        assert "Workspace settings" in settings
        assert "Members" in members


def test_unexpected_member_errors_use_the_generic_message(
    owner: OwnerApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise WorkspaceError

    monkeypatch.setattr(workspace_routes, "change_role", fail)
    mine = owner.membership_id(owner.admin_id)
    response = owner.post(f"/workspace/members/{mine}/role", {"role": "owner"})
    assert response.status_code == 409
    assert "The change could not be made. Reload the page and try again." in response.text
    assert LAST_OWNER not in response.text
