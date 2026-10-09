from __future__ import annotations

import typer

from assetboard.app import create_app
from assetboard.branding import APP_NAME, CLI_NAME
from assetboard.config import Settings
from assetboard.db.migrate import check_database_migrations, create_revision, upgrade_database

app = typer.Typer(name=CLI_NAME, help=f"{APP_NAME} command-line interface")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Host interface to bind."),
    port: int = typer.Option(8000, help="Port to listen on."),
) -> None:
    """Start the development web server."""
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port)


@app.command()
def migrate() -> None:
    """Run database migrations."""
    settings = Settings()
    upgrade_database(settings)
    typer.echo("Database migrations applied.")


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
