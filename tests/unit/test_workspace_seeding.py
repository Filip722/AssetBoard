"""Every new workspace gets the suggested categories (pre-release A).

One test per way a workspace is created (first-run setup, open registration, ``create-admin``,
OIDC and proxy auto-create), the ways that create none (invitation sign-up, an administrator's
new account, ``create-admin --no-workspace``), and the shared building blocks.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from assetboard.branding import ENV_PREFIX
from assetboard.cli import app as cli_app
from assetboard.config import Settings
from assetboard.db.models.identity import AuditEvent, Membership, SetupState, User, Workspace
from assetboard.db.models.ledger import Category
from assetboard.db.repositories import CategoryRepository, create_owned_workspace
from assetboard.domain.categories import SEED
from assetboard.i18n import gettext
from assetboard.services import auth, categories
from assetboard.services.auth import (
    WorkspaceCreationFailed,
    argon2_hash,
    create_user_with_temporary_password,
    register_with_invitation,
    run_initial_setup,
)
from assetboard.services.external_identity import PROXY_PROVIDER, SignInIdentity
from assetboard.services.oidc_accounts import sign_in_external
from assetboard.services.workspace_options import validate_workspace_options
from assetboard.services.workspaces import create_invitation
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

PASSWORD = "Seeding Strong Password"
ISSUER = "https://idp.example.test"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


def _close_setup(db: WorkspaceDb) -> None:
    with db.factory.begin() as session:
        session.execute(update(SetupState).values(completed_at=db.clock.now()))


def _categories(db: WorkspaceDb, workspace_id: UUID) -> list[tuple[str, str, str, int]]:
    with db.factory() as session:
        rows = session.scalars(
            select(Category)
            .where(Category.workspace_id == workspace_id)
            .order_by(Category.kind, Category.sort_order)
        )
        return [(c.kind, c.treatment, c.name, c.sort_order) for c in rows]


def _expected(locale: str) -> list[tuple[str, str, str, int]]:
    positions: dict[str, int] = {}
    rows: list[tuple[str, str, str, int]] = []
    for seed in SEED:
        position = positions.get(seed.kind.value, 0)
        positions[seed.kind.value] = position + 1
        rows.append((seed.kind.value, seed.treatment.value, gettext(seed.name, locale), position))
    return sorted(rows, key=lambda row: (row[0], row[3]))


def _seed_events(db: WorkspaceDb, workspace_id: UUID) -> list[tuple[str, UUID | None, Any]]:
    with db.factory() as session:
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.workspace_id == workspace_id, AuditEvent.action == "category.seeded"
            )
        )
        return [(e.actor_type, e.actor_user_id, e.data) for e in events]


def _owned_workspace(db: WorkspaceDb, email: str) -> UUID:
    with db.factory() as session:
        return session.scalars(
            select(Membership.workspace_id)
            .join(User, User.id == Membership.user_id)
            .where(User.email == email, Membership.role == "owner")
        ).one()


def _workspace_count(db: WorkspaceDb) -> int:
    with db.factory() as session:
        return len(session.scalars(select(Workspace.id)).all())


def test_polish_seed_names_differ_from_english() -> None:
    # Guards the language assertions below: they would pass vacuously without a translation.
    assert _expected("pl") != _expected("en")


# Paths that create a workspace


@pytest.mark.parametrize("locale", ["en", "pl"])
def test_first_run_setup_seeds_in_the_page_language(
    db: WorkspaceDb, tmp_path: Path, locale: str
) -> None:
    user, workspace = run_initial_setup(
        db.factory,
        email="admin@example.com",
        display_name="Admin",
        password=PASSWORD,
        workspace_name="Setup Home",
        settings=Settings(_env_file=None, data_dir=tmp_path),
        clock=db.clock,
        workspace_locale="en",
        seed_locale=locale,
    )
    assert _categories(db, workspace.id) == _expected(locale)
    # Setup is a system action: no signed-in actor yet.
    assert _seed_events(db, workspace.id) == [
        ("system", None, {"count": len(SEED), "automatic": True})
    ]
    assert user.id is not None


def test_without_a_seed_language_the_workspace_locale_is_used(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    _user, workspace = run_initial_setup(
        db.factory,
        email="admin@example.com",
        display_name="Admin",
        password=PASSWORD,
        workspace_name="Dom",
        settings=Settings(_env_file=None, data_dir=tmp_path),
        clock=db.clock,
        workspace_locale="pl",
    )
    assert _categories(db, workspace.id) == _expected("pl")


def test_open_registration_seeds_as_the_new_user(db: WorkspaceDb, tmp_path: Path) -> None:
    _close_setup(db)
    settings = Settings(_env_file=None, data_dir=tmp_path, allow_registration=True)
    outcome = auth.register_with_factory(
        db.factory,
        email="reg@example.com",
        display_name="Registrant",
        password=PASSWORD,
        workspace_name="Budżet",
        settings=settings,
        clock=db.clock,
        host="192.0.2.1",
        workspace_locale="en",
        seed_locale="pl",
    )
    assert outcome.status == "created" and outcome.user and outcome.workspace
    assert _categories(db, outcome.workspace.id) == _expected("pl")
    assert _seed_events(db, outcome.workspace.id) == [
        ("user", outcome.user.id, {"count": len(SEED), "automatic": True})
    ]


def _cli(tmp_path: Path, *extra: str) -> Any:
    env = {f"{ENV_PREFIX}_DATA_DIR": str(tmp_path / "cli-data")}
    return CliRunner().invoke(
        cli_app,
        [
            "create-admin",
            "--email",
            "cli@example.com",
            "--display-name",
            "CLI Admin",
            *extra,
            "--password-stdin",
        ],
        input=f"{PASSWORD}\n",
        env=env,
    )


def _cli_categories(tmp_path: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    connection = sqlite3.connect(tmp_path / "cli-data" / "assetboard.db")
    try:
        names = connection.execute(
            "SELECT kind, name FROM categories ORDER BY kind, sort_order"
        ).fetchall()
        events = connection.execute(
            "SELECT actor_type, action FROM audit_events WHERE action = 'category.seeded'"
        ).fetchall()
        return names, events
    finally:
        connection.close()


@pytest.mark.parametrize(("extra", "locale"), [((), "en"), (("--locale", "pl"), "pl")])
def test_create_admin_seeds_its_workspace_in_the_locale_option(
    tmp_path: Path, extra: tuple[str, ...], locale: str
) -> None:
    result = _cli(tmp_path, "--workspace-name", "Home", *extra)
    assert result.exit_code == 0, result.output
    names, events = _cli_categories(tmp_path)
    assert names == [(kind, name) for kind, _treatment, name, _order in _expected(locale)]
    assert events == [("cli", "category.seeded")]


def test_create_admin_without_a_workspace_seeds_nothing(tmp_path: Path) -> None:
    result = _cli(tmp_path, "--no-workspace")
    assert result.exit_code == 0, result.output
    assert _cli_categories(tmp_path) == ([], [])


def _identity(issuer: str, subject: str, email: str) -> SignInIdentity:
    return SignInIdentity(
        issuer=issuer,
        subject=subject,
        email=email,
        email_verified=True,
        display_name="Ola",
        groups=None,
    )


def _external_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "data_dir": tmp_path,
        "env": "prod",
        "base_url": "https://money.example.test",
        "oidc_issuer": ISSUER,
        "oidc_client_id": "assetboard",
        "oidc_client_secret": "secret",
        "oidc_auto_create": True,
        "proxy_auth_enabled": True,
        "proxy_auth_user_header": "Remote-User",
        "proxy_auth_email_header": "Remote-Email",
        "proxy_auth_auto_create": True,
        "trusted_proxies": "172.20.0.2",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize("issuer", [ISSUER, PROXY_PROVIDER])
def test_oidc_and_proxy_auto_create_seed_in_the_request_language(
    db: WorkspaceDb, tmp_path: Path, issuer: str
) -> None:
    _close_setup(db)
    outcome = sign_in_external(
        db.factory,
        _identity(issuer, "ola", "ola@example.com"),
        next_path="/",
        settings=_external_settings(tmp_path),
        clock=db.clock,
        host="203.0.113.7",
        locale="pl",
    )
    assert outcome.status == "signed_in" and outcome.created
    workspace_id = _owned_workspace(db, "ola@example.com")
    assert _categories(db, workspace_id) == _expected("pl")
    assert _seed_events(db, workspace_id) == [
        ("user", outcome.user_id, {"count": len(SEED), "automatic": True})
    ]


# Paths that create no workspace


def test_invitation_sign_up_creates_and_seeds_no_workspace(db: WorkspaceDb, tmp_path: Path) -> None:
    _close_setup(db)
    db.add_member("olga", "owner")
    token = create_invitation(
        db.factory,
        db.ctx("olga", "owner"),
        role="viewer",
        email="newbie@example.com",
        expires_in_days=7,
    ).token
    before = _workspace_count(db)
    outcome = register_with_invitation(
        db.factory,
        token=token,
        email="newbie@example.com",
        display_name="Newbie",
        password=PASSWORD,
        settings=Settings(_env_file=None, data_dir=tmp_path),
        clock=db.clock,
        host="192.0.2.2",
    )
    assert outcome.status == "created" and outcome.workspace is None
    assert _workspace_count(db) == before
    assert _categories(db, db.workspace_id) == []  # the invitation's workspace is untouched


def test_an_administrators_new_account_gets_no_workspace(db: WorkspaceDb, tmp_path: Path) -> None:
    before = _workspace_count(db)
    with db.factory.begin() as session:
        create_user_with_temporary_password(
            session,
            email="temp@example.com",
            display_name="Temp",
            password=PASSWORD,
            password_hash=argon2_hash(PASSWORD),
            settings=Settings(_env_file=None, data_dir=tmp_path),
        )
    assert _workspace_count(db) == before


# Building blocks


def test_a_new_workspace_and_its_categories_roll_back_together(
    db: WorkspaceDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("seed failed")

    monkeypatch.setattr(categories, "seed_into", fail)
    before = _workspace_count(db)
    with pytest.raises(RuntimeError, match="seed failed"):
        run_initial_setup(
            db.factory,
            email="admin@example.com",
            display_name="Admin",
            password=PASSWORD,
            workspace_name="Home 2",
            settings=Settings(_env_file=None, data_dir=tmp_path),
            clock=db.clock,
        )
    assert _workspace_count(db) == before
    with db.factory() as session:
        assert session.scalars(select(User.id)).all() == []


def test_a_workspace_cannot_be_created_without_its_seed(db: WorkspaceDb, tmp_path: Path) -> None:
    options = validate_workspace_options(
        base_currency="EUR", country_code=None, timezone="UTC", locale="en"
    )
    with db.factory() as session, pytest.raises(RuntimeError, match="category seed"):
        auth._create_user_records(  # pyright: ignore[reportPrivateUsage]
            session,
            email="x@example.com",
            display_name="X",
            password=None,
            workspace_name=None,
            options=options,
            settings=Settings(_env_file=None, data_dir=tmp_path),
            is_admin=False,
        )


def test_create_owned_workspace_adds_the_owner_in_the_new_workspace_only(
    db: WorkspaceDb,
) -> None:
    db.add_member("olga", "owner")
    owner = db.users["olga"]
    with db.factory.begin() as session:
        workspace, scope = create_owned_workspace(
            session,
            name="Second",
            base_currency="PLN",
            country_code="PL",
            timezone="Europe/Warsaw",
            locale="pl",
            owner_user_id=owner,
        )
        assert scope.workspace.id == workspace.id
        new_id = workspace.id
        # The scope writes into the new workspace only.
        assert CategoryRepository(session, scope).count() == 0
    assert db.roles(new_id) == {"olga": "owner"}
    assert db.roles() == {"olga": "owner"}  # still a member of Home, unchanged


def test_seed_into_writes_only_into_its_scope(db: WorkspaceDb) -> None:
    db.add_member("olga", "owner")
    owner = db.users["olga"]
    with db.factory.begin() as session:
        workspace, scope = create_owned_workspace(
            session,
            name="Seeded",
            base_currency="EUR",
            country_code=None,
            timezone="UTC",
            locale="en",
            owner_user_id=owner,
        )
        count = categories.seed_into(
            session,
            scope,
            locale="en",
            actor_user_id=owner,
            actor_type="user",
            now=db.clock.now(),
            automatic=True,
        )
        new_id = workspace.id
    assert count == len(SEED)
    assert _categories(db, new_id) == _expected("en")
    assert _categories(db, db.workspace_id) == []


def _failing_seed(*_args: object, **_kwargs: object) -> int:
    raise IntegrityError("INSERT INTO categories", {}, Exception("UNIQUE constraint failed"))


def test_a_seed_constraint_error_in_registration_is_not_reported_as_a_taken_email(
    db: WorkspaceDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _close_setup(db)
    monkeypatch.setattr(categories, "seed_into", _failing_seed)
    with pytest.raises(WorkspaceCreationFailed) as failed:
        auth.register_with_factory(
            db.factory,
            email="reg@example.com",
            display_name="Registrant",
            password=PASSWORD,
            workspace_name="Mine",
            settings=Settings(_env_file=None, data_dir=tmp_path, allow_registration=True),
            clock=db.clock,
            host="192.0.2.3",
        )
    assert isinstance(failed.value.__cause__, IntegrityError)
    with db.factory() as session:
        assert session.scalars(select(User.id)).all() == []


def test_a_seed_constraint_error_in_auto_create_is_not_reported_as_an_existing_account(
    db: WorkspaceDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _close_setup(db)
    monkeypatch.setattr(categories, "seed_into", _failing_seed)
    with pytest.raises(WorkspaceCreationFailed):
        sign_in_external(
            db.factory,
            _identity(ISSUER, "ola", "ola@example.com"),
            next_path="/",
            settings=_external_settings(tmp_path),
            clock=db.clock,
            host="203.0.113.8",
        )
    with db.factory() as session:
        assert session.scalars(select(User.id)).all() == []
