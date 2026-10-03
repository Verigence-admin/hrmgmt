from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest import alembic_config


def test_baseline_creates_tables_and_seeds_four_designations(migrated_engine):
    with migrated_engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='hr'")
            )
        }
        assert {"audit_log", "designation", "alembic_version"} <= tables
        rows = conn.execute(
            text("SELECT code, label FROM hr.designation ORDER BY sort_order")
        ).all()
    assert [r[1] for r in rows] == ["Auditor", "Senior Auditor", "Assistant Manager", "Manager"]


def test_upgrade_is_repeatable(migrated_engine):
    command.upgrade(alembic_config(), "head")
    with migrated_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM hr.designation")).scalar_one() == 4


def test_audit_log_is_append_only(migrated_engine):
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hr.audit_log (actor_user_id, action, entity_type, entity_id)"
                " VALUES ('u', 'TEST', 'test', 'append-only-check')"
            )
        )
    for statement in (
        "UPDATE hr.audit_log SET action = 'X' WHERE entity_id = 'append-only-check'",
        "DELETE FROM hr.audit_log WHERE entity_id = 'append-only-check'",
        "TRUNCATE hr.audit_log",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            with migrated_engine.begin() as conn:
                conn.execute(text(statement))


def test_unknown_recorded_revision_is_refused(migrated_engine):
    """The database knows a revision this repository does not: Alembic must stop, not guess."""
    with migrated_engine.begin() as conn:
        conn.execute(text("UPDATE hr.alembic_version SET version_num = '9999_from_the_future'"))
    try:
        with pytest.raises(Exception, match="9999_from_the_future"):
            command.upgrade(alembic_config(), "head")
    finally:
        with migrated_engine.begin() as conn:
            conn.execute(text("UPDATE hr.alembic_version SET version_num = '0001_baseline'"))


def test_downgrade_is_refused():
    with pytest.raises(RuntimeError, match="forward-only"):
        command.downgrade(alembic_config(), "base")
