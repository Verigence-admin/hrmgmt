from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, text

from hrmgmt.config import normalise_database_url

config = context.config

# HR owns the `hr` schema in the shared database. Its Alembic history lives inside that schema
# (hr.alembic_version), separate from every other service's history. If the database records a
# revision this repository does not contain, Alembic refuses to run: it never guesses.
VERSION_TABLE = "alembic_version"
VERSION_SCHEMA = "hr"


def _url() -> str:
    raw = os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        raise RuntimeError("DATABASE_URL is required to run HR migrations")
    return normalise_database_url(raw)


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        # The version table is created inside `hr`, so the schema must exist first.
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS hr"))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=None,
            version_table=VERSION_TABLE,
            version_table_schema=VERSION_SCHEMA,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
