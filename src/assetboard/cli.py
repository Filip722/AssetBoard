from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import typer
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import ASGIApp

from assetboard.app import create_app
from assetboard.branding import APP_NAME, CLI_NAME
from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.engine import create_database_engine
from assetboard.db.migrate import (
    check_database_migrations,
    create_revision,
    database_is_at_head,
    upgrade_database,
)
from assetboard.middleware import PeerMiddleware
from assetboard.services.auth import EmailAlreadyRegistered, create_admin_cli, reset_user_password

app = typer.Typer(name=CLI_NAME, help=f"{APP_NAME} command-line interface")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Host interface to bind."),
    port: int = typer.Option(8000, help="Port to listen on."),
) -> None:
    """Start the web server."""
    import uvicorn

    try:
        settings = Settings()
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    try:
        application = create_app(settings)
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    uvicorn.run(serve_app(application, settings), host=host, port=port, **server_options(settings))


def serve_app(application: ASGIApp, settings: Settings) -> ASGIApp:
    """The application as ``serve`` runs it: ``PeerMiddleware`` outermost (ADR-0015)."""
    return PeerMiddleware(application, settings.trusted_proxies)


def dev_app() -> ASGIApp:
    """``make dev``'s factory (uvicorn ``--reload`` needs one): wired exactly as ``serve``.

    The Makefile also passes ``--no-proxy-headers`` and ``--no-access-log``, the command-line
    equivalents of ``server_options``.
    """
    settings = Settings()
    return serve_app(create_app(settings), settings)


def server_options(settings: Settings) -> dict[str, Any]:
    """Uvicorn options for ``serve``.

    The access log is off: the application's own ``request_complete`` line covers every request
    and redacts invitation tokens, while uvicorn's access log would print full URLs, including
    query strings such as ``/login?next=/invite/<token>`` (ADR-0013).

    Uvicorn's own forwarded-header handling is off too: ``PeerMiddleware`` (``serve_app``)
    applies forwarded headers from ``TRUSTED_PROXIES`` with uvicorn's ``ProxyHeadersMiddleware``,
    after recording the TCP peer that proxy authentication checks (ADR-0015). The empty
    ``forwarded_allow_ips`` keeps uvicorn from falling back to the ``FORWARDED_ALLOW_IPS``
    environment variable or ``127.0.0.1``.
    """
    del settings  # TRUSTED_PROXIES is applied by serve_app
    return {"access_log": False, "proxy_headers": False, "forwarded_allow_ips": ""}


@app.command()
def migrate() -> None:
    """Run database migrations."""
    settings = Settings()
    upgrade_database(settings)
    typer.echo("Database migrations applied.")


NOT_AT_HEAD_MESSAGE = "database is not at the latest schema; run `assetboard migrate` first"


class CommandError(Exception):
    pass


SQLITE_EMAIL_CONFLICT = "UNIQUE constraint failed: users.email"
POSTGRES_EMAIL_CONSTRAINT = "uq_users_email_normalized"


def is_email_conflict(error: IntegrityError) -> bool:
    """True only for the users email unique constraint.

    SQLite reports the exact column (``UNIQUE constraint failed: users.email``); PostgreSQL
    reports the constraint name.
    """
    message = str(error.orig)
    return message == SQLITE_EMAIL_CONFLICT or POSTGRES_EMAIL_CONSTRAINT in message


def _read_password(password_stdin: bool) -> str:
    if password_stdin:
        password = sys.stdin.read().rstrip("\r\n")
    else:
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    if not password:
        raise CommandError("Password must not be empty")
    return password


def _ensure_schema(settings: Settings) -> None:
    """Migrate like app startup when AUTO_MIGRATE is on; otherwise require the latest schema."""
    if settings.auto_migrate:
        settings.ensure_data_dir_writable()
        upgrade_database(settings)
    elif not database_is_at_head(settings):
        raise CommandError(NOT_AT_HEAD_MESSAGE)


def _with_database[T](settings: Settings, operation: Callable[[Session], T]) -> T:
    """Run ``operation`` in one transaction."""
    if settings.database_url is None:
        raise CommandError("DATABASE_URL was not configured")
    engine = create_database_engine(settings.database_url)
    clock = Clock()
    factory = sessionmaker(engine, class_=Session, info={"clock": clock}, expire_on_commit=False)
    try:
        with factory.begin() as session:
            return operation(session)
    finally:
        engine.dispose()


@app.command("create-admin")
def create_admin_command(
    email: str | None = typer.Option(None, "--email"),
    display_name: str | None = typer.Option(None, "--display-name"),
    workspace_name: str | None = typer.Option(None, "--workspace-name"),
    base_currency: str | None = typer.Option(None, "--currency", help="ISO currency code."),
    country: str | None = typer.Option(None, "--country", help="ISO country code."),
    timezone: str | None = typer.Option(None, "--timezone", help="IANA time zone."),
    locale: str | None = typer.Option(
        None,
        "--locale",
        help="Workspace locale; the suggested categories are named in it.",
    ),
    no_workspace: bool = typer.Option(False, "--no-workspace", help="Create only the user."),
    password_stdin: bool = typer.Option(False, "--password-stdin"),
) -> None:
    """Create an instance administrator (break-glass; never modifies an existing user)."""
    try:
        settings = Settings()
        _ensure_schema(settings)
        if password_stdin:
            # Non-interactive: stdin carries only the password, so nothing else is prompted.
            if email is None or display_name is None:
                raise CommandError("--email and --display-name are required with --password-stdin")
            email_value, name_value = email, display_name
            workspace_value = workspace_name or name_value
        else:
            email_value = email or typer.prompt("Email")
            name_value = display_name or typer.prompt("Display name")
            workspace_value = workspace_name
            if workspace_value is None and not no_workspace:
                workspace_value = typer.prompt("Workspace name", default=name_value)
        password = _read_password(password_stdin)
        user, workspace = _with_database(
            settings,
            lambda session: create_admin_cli(
                session,
                email=email_value,
                display_name=name_value,
                password=password,
                settings=settings,
                clock=session.info["clock"],
                workspace_name=None if no_workspace else workspace_value,
                create_workspace=not no_workspace,
                base_currency=base_currency,
                country_code=country,
                timezone=timezone,
                workspace_locale=locale,
            ),
        )
    except IntegrityError as exc:
        # The service checks first; this covers a concurrent insert of the same email.
        if not is_email_conflict(exc):
            raise
        typer.echo(EmailAlreadyRegistered().args[0], err=True)
        raise typer.Exit(code=1) from exc
    except (CommandError, ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if workspace is None:
        typer.echo(f"Created administrator {user.email}.")
    else:
        typer.echo(f"Created administrator {user.email} and workspace {workspace.name}.")


@app.command("reset-password")
def reset_password_command(
    email: str = typer.Argument(..., help="Email address of the user."),
    password_stdin: bool = typer.Option(False, "--password-stdin"),
) -> None:
    """Set a new password for a local user and revoke all of their sessions."""
    try:
        settings = Settings()
        _ensure_schema(settings)
        password = _read_password(password_stdin)
        user = _with_database(
            settings,
            lambda session: reset_user_password(
                session,
                email=email,
                password=password,
                settings=settings,
                clock=session.info["clock"],
            ),
        )
    except (CommandError, ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Password reset for {user.email}.")


@app.command("check-migrations")
def check_migrations() -> None:
    """Check for model changes that need a migration."""
    check_database_migrations(Settings())


@app.command(hidden=True)
def revision(message: str = typer.Option(..., "--message", "-m")) -> None:
    """Create an autogenerate migration after validating the current schema."""
    create_revision(Settings(), message)


if __name__ == "__main__":
    app()
