"""Self-service workspaces over HTTP (pre-release A): the "No workspace" page, the switcher
link, ``/workspaces/new`` and the administrator's instance setting."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from assetboard.db.models.identity import (
    AuditEvent,
    InstanceSettings,
    Membership,
    User,
    Workspace,
)
from assetboard.db.models.ledger import Category
from assetboard.domain.categories import SEED
from assetboard.i18n import gettext
from tests.conftest import ADMIN_PASSWORD, OwnerApp

NO_WORKSPACE = "Your account is not a member of any workspace."


@contextmanager
def signed_in_as(owner: OwnerApp, key: str) -> Iterator[TestClient]:
    with TestClient(owner.app) as client:
        response = owner.web.sign_in(client, email=f"{key}@example.com")
        assert response.status_code == 303, response.text
        yield client


def _set_allowed(owner: OwnerApp, allowed: bool) -> None:
    with owner.db() as session, session.begin():
        session.execute(update(InstanceSettings).values(allow_workspace_creation=allowed))


def _form(client: TestClient, owner: OwnerApp, **values: str) -> dict[str, str]:
    token = owner.web.csrf(client.get("/workspaces/new").text)
    return {
        "csrf_token": token,
        "workspace_name": "Bea's money",
        "base_currency": "PLN",
        "country_code": "PL",
        "timezone": "Europe/Warsaw",
        "workspace_locale": "pl",
    } | values


def _owned(owner: OwnerApp, user_id: UUID) -> list[Workspace]:
    with owner.db() as session:
        return list(
            session.scalars(
                select(Workspace)
                .join(Membership, Membership.workspace_id == Workspace.id)
                .where(Membership.user_id == user_id, Membership.role == "owner")
            )
        )


def _workspace_count(owner: OwnerApp) -> int:
    with owner.db() as session:
        return len(session.scalars(select(Workspace.id)).all())


def _csp_safe(html: str) -> None:
    assert " style=" not in html
    assert re.search(r"<script(?![^>]*\ssrc=)", html) is None


# The "No workspace" page


def test_a_user_without_a_workspace_is_offered_to_create_one(owner: OwnerApp) -> None:
    owner.add_user("bea")
    with signed_in_as(owner, "bea") as client:
        page = client.get("/")
    assert page.status_code == 403
    assert NO_WORKSPACE in page.text
    assert "data-create-workspace" in page.text and 'href="/workspaces/new"' in page.text
    _csp_safe(page.text)


def test_the_offer_is_hidden_when_creation_is_off(owner: OwnerApp) -> None:
    bea = owner.add_user("bea")
    _set_allowed(owner, False)
    before = _workspace_count(owner)
    with signed_in_as(owner, "bea") as client:
        page = client.get("/")
        assert page.status_code == 403 and NO_WORKSPACE in page.text
        assert "data-create-workspace" not in page.text
        assert client.get("/workspaces/new").status_code == 404
        csrf = owner.web.csrf(client.get("/account").text)
        response = client.post(
            "/workspaces/new",
            data={"csrf_token": csrf, "workspace_name": "X", "base_currency": "EUR",
                  "timezone": "UTC", "workspace_locale": "en"},
        )  # fmt: skip
        assert response.status_code == 404
    assert _owned(owner, bea) == []
    assert _workspace_count(owner) == before


def test_an_htmx_request_without_a_workspace_still_gets_a_toast(owner: OwnerApp) -> None:
    owner.add_user("bea")
    with signed_in_as(owner, "bea") as client:
        response = client.get("/", headers={"HX-Request": "true"})
    assert response.status_code == 403
    assert response.headers["HX-Retarget"] == "#toast-container"
    assert "data-create-workspace" not in response.text


# Creating


def test_create_a_workspace_switches_to_it_with_the_suggested_categories(owner: OwnerApp) -> None:
    bea = owner.add_user("bea")
    with signed_in_as(owner, "bea") as client:
        client.headers["accept-language"] = "pl"
        form_page = client.get("/workspaces/new")
        assert form_page.status_code == 200
        assert 'name="workspace_name" type="text" value="Bea"' in form_page.text  # prefilled
        _csp_safe(form_page.text)
        response = client.post("/workspaces/new", data=_form(client, owner), follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (303, "/")
        home = client.get("/")
        assert home.status_code == 200
        assert "data-current-workspace>Bea&#39;s money<" in home.text
        categories_page = client.get("/categories").text
    [workspace] = _owned(owner, bea)
    assert (workspace.base_currency, workspace.country_code, workspace.locale) == (
        "PLN",
        "PL",
        "pl",
    )
    with owner.db() as session:
        names = session.scalars(
            select(Category.name).where(Category.workspace_id == workspace.id)
        ).all()
    assert len(names) == len(SEED) and gettext("Food", "pl") in names
    assert f"data-category-name>{gettext('Food', 'pl')}<" in categories_page


def test_a_blank_name_uses_the_display_name(owner: OwnerApp) -> None:
    bea = owner.add_user("bea")
    with signed_in_as(owner, "bea") as client:
        response = client.post(
            "/workspaces/new", data=_form(client, owner, workspace_name=" "), follow_redirects=False
        )
    assert response.status_code == 303
    assert [w.name for w in _owned(owner, bea)] == ["Bea"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("workspace_name", "x" * 121, "Enter a workspace name of 1 to 120 characters."),
        ("base_currency", "XXX", "Choose a supported currency."),
        ("country_code", "ZZ", "Choose a country from the list."),
        ("timezone", "Mars/Base", "Choose a time zone from the list."),
        ("workspace_locale", "xx", "Choose a supported language."),
    ],
)
def test_invalid_values_are_shown_on_the_field(
    owner: OwnerApp, field: str, value: str, message: str
) -> None:
    bea = owner.add_user("bea")
    with signed_in_as(owner, "bea") as client:
        response = client.post("/workspaces/new", data=_form(client, owner, **{field: value}))
    assert response.status_code == 400
    assert message in response.text and f'aria-describedby="{field}-error"' in response.text
    assert _owned(owner, bea) == []


def test_creating_needs_a_csrf_token(owner: OwnerApp) -> None:
    bea = owner.add_user("bea")
    with signed_in_as(owner, "bea") as client:
        form = _form(client, owner)
        del form["csrf_token"]
        assert client.post("/workspaces/new", data=form).status_code == 403
    assert _owned(owner, bea) == []


def test_instance_administrators_can_create_while_creation_is_off(owner: OwnerApp) -> None:
    _set_allowed(owner, False)
    assert owner.client.get("/workspaces/new").status_code == 200
    form = _form(owner.client, owner, workspace_name="Admin's second")
    response = owner.client.post("/workspaces/new", data=form, follow_redirects=False)
    assert response.status_code == 303
    assert len(_owned(owner, owner.admin_id)) == 2


def test_a_temporary_password_must_be_changed_first(owner: OwnerApp) -> None:
    bea = owner.add_user("bea")
    with owner.db() as session, session.begin():
        session.execute(update(User).where(User.id == bea).values(must_change_password=True))
    with TestClient(owner.app) as client:
        owner.web.sign_in(client, email="bea@example.com")
        response = client.get("/workspaces/new", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/account/password")


# The switcher link


def test_the_page_header_offers_a_new_workspace(owner: OwnerApp) -> None:
    bea = owner.add_user("bea")
    owner.join(bea, owner.home_id, "editor")
    with signed_in_as(owner, "bea") as client:
        assert "data-new-workspace-link" in client.get("/").text  # one membership
        client.post("/workspaces/new", data=_form(client, owner), follow_redirects=False)
        page = client.get("/").text
        assert "data-workspace-switcher" in page and "data-new-workspace-link" in page
        _set_allowed(owner, False)
        assert "data-new-workspace-link" not in client.get("/").text


# The instance setting (administrators)


def test_the_settings_page_is_for_instance_administrators(owner: OwnerApp) -> None:
    page = owner.client.get("/admin/settings")
    assert page.status_code == 200
    assert 'value="on" selected' in page.text
    assert "data-admin-settings" in owner.client.get("/admin/users").text
    assert "data-admin-settings-link" in owner.client.get("/").text
    _csp_safe(page.text)
    owner.add_user("bea")
    with signed_in_as(owner, "bea") as client:
        assert client.get("/admin/settings").status_code == 403
        token = owner.web.csrf(client.get("/account").text)
        response = client.post(
            "/admin/settings",
            data={"csrf_token": token, "allow_workspace_creation": "off",
                  "confirm_password": ADMIN_PASSWORD},
        )  # fmt: skip
        assert response.status_code == 403
        assert "data-admin-settings-link" not in client.get("/account").text
    assert _allowed(owner) is True


def _allowed(owner: OwnerApp) -> bool:
    with owner.db() as session:
        row = session.get(InstanceSettings, 1)
        assert row is not None
        return row.allow_workspace_creation


def _save(owner: OwnerApp, value: str, password: str = ADMIN_PASSWORD) -> Any:
    return owner.post(
        "/admin/settings",
        {"allow_workspace_creation": value, "confirm_password": password},
        token_from="/admin/settings",
    )


def test_changing_the_setting_needs_the_password_and_is_audited(owner: OwnerApp) -> None:
    wrong = _save(owner, "off", password="not the password")
    assert wrong.status_code == 401
    assert _allowed(owner) is True
    saved = _save(owner, "off")
    assert saved.status_code == 200
    assert "Instance settings saved." in saved.text
    assert 'value="off" selected' in saved.text
    assert _allowed(owner) is False
    unchanged = _save(owner, "off")
    assert "Nothing to change" in unchanged.text
    with owner.db() as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "instance.settings_updated")
        ).all()
    assert [(e.workspace_id, e.data) for e in events] == [
        (None, {"allow_workspace_creation": False})
    ]
    page = owner.client.get("/audit?scope=instance&group=instance_settings").text
    assert "Instance settings changed" in page
    assert "Every signed-in user can create workspaces: no" in page
    owner.client.headers["Accept-Language"] = "pl"
    page = owner.client.get("/audit?scope=instance&group=instance_settings").text
    assert "Zmieniono ustawienia instancji" in page


def test_an_unknown_setting_value_is_refused(owner: OwnerApp) -> None:
    response = _save(owner, "maybe")
    assert response.status_code == 400
    assert "Choose who can create workspaces." in response.text
    assert _allowed(owner) is True


def test_the_creation_appears_in_the_new_workspace_log(owner: OwnerApp) -> None:
    form = _form(owner.client, owner, workspace_name="Second")
    assert owner.client.post("/workspaces/new", data=form).status_code == 200
    page = owner.client.get("/audit?scope=workspace&group=settings").text
    assert "Workspace created" in page
    owner.client.headers["Accept-Language"] = "pl"
    assert "Utworzono obszar roboczy" in owner.client.get("/audit?scope=workspace").text


def test_new_pages_render_in_polish(owner: OwnerApp) -> None:
    owner.client.headers["Accept-Language"] = "pl"
    assert "Utwórz obszar roboczy" in owner.client.get("/workspaces/new").text
    page = owner.client.get("/admin/settings").text
    assert "Kto może tworzyć obszary robocze" in page and "Ustawienia instancji" in page
