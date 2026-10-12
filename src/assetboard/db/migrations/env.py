from __future__ import annotations

from alembic import context
from sqlalchemy.engine import Connection, make_url

import assetboard.db.models  # noqa: F401
from assetboard.db.base import Base
from assetboard.db.engine import create_database_engine
from assetboard.db.migrate import configured_database_url, render_item

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    database_url = configured_database_url(config)
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=make_url(database_url).get_backend_name() == "sqlite",
        user_module_prefix="assetboard.db.types.",
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_database_engine(configured_database_url(config))
    try:
        with engine.connect() as connection:
            sqlite = connection.dialect.name == "sqlite"
            if sqlite:
                # SQLite rebuilds a table to change a column (batch mode): with foreign keys on,
                # dropping the old table would cascade-delete every row that references it.
                # SQLite's documented procedure: enforcement off around the change (set outside
                # a transaction, on the driver connection), then PRAGMA foreign_key_check before
                # committing.
                _driver_execute(connection, "PRAGMA foreign_keys=OFF")
            try:
                configure_context(connection)
                with context.begin_transaction():
                    context.run_migrations()
                    if sqlite:
                        broken = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                        if broken:
                            # Rows: (table, rowid, referenced table, foreign key index).
                            pairs = sorted({f"{row[0]} -> {row[2]}" for row in broken})
                            raise RuntimeError(
                                f"migration left {len(broken)} broken foreign key reference(s): "
                                + ", ".join(pairs)
                            )
            finally:
                if sqlite:
                    _driver_execute(connection, "PRAGMA foreign_keys=ON")
    finally:
        engine.dispose()


def _driver_execute(connection: Connection, statement: str) -> None:
    driver = connection.connection.driver_connection
    if driver is None:
        raise RuntimeError("no driver connection")
    driver.execute(statement)


def configure_context(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
        user_module_prefix="assetboard.db.types.",
        render_item=render_item,
    )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
