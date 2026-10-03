from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Connection, Engine, create_engine

from hrmgmt.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    # Every SQL statement in this service is schema-qualified (hr.<table>) so nothing
    # depends on search_path; that keeps it safe behind a pooled Neon endpoint.
    return create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=5,
        pool_pre_ping=True,
    )


def get_conn() -> Iterator[Connection]:
    """One transaction per request: committed when the handler returns, rolled back on any error.
    A handler that must call another service may `conn.commit()` first, so the database is
    never held in an open transaction across a network call."""
    with get_engine().connect() as conn:
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
