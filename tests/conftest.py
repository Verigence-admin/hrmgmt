from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://audit:audit@localhost:5432/hrtest"
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL


def alembic_config() -> Config:
    return Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))


@pytest.fixture(scope="session")
def migrated_engine():
    """A database whose hr schema was built from scratch by the migrations."""
    from hrmgmt.config import normalise_database_url

    engine = create_engine(normalise_database_url(TEST_DATABASE_URL))
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS hr CASCADE"))
    command.upgrade(alembic_config(), "head")
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _a_face_is_present(monkeypatch):
    """Test photos are plain colour blocks. Unless a test says otherwise, the face check finds a face."""
    monkeypatch.setattr("hrmgmt.api.attendance.face_present", lambda image: True)


@pytest.fixture(autouse=True)
def _forget_remembered_denials():
    """/me remembers "no" answers for a short time; every test starts with none remembered."""
    from hrmgmt.api import meta

    meta._DENIED.clear()
    yield
    meta._DENIED.clear()
