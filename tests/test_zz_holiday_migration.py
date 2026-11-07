"""Migration 0025 keeps every existing holiday as a full-day, company-wide one (7 Nov 2026).

Named to run last: it rebuilds the hr schema from scratch, stopping at 0024 to put the old rows in,
and leaves it at head again."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import text

from tests.conftest import alembic_config


def test_existing_holidays_become_full_day_company_wide_with_nothing_lost(migrated_engine):
    with migrated_engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS hr CASCADE"))
    command.upgrade(alembic_config(), "0024_attendance_correction")
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hr.holiday (holiday_date, name, status, updated_at, updated_by)"
                " VALUES ('2026-12-31', 'Year end', 'DECLARED', '2026-11-01T10:00:00+05:30', 'u-1')"
            )
        )
        before = conn.execute(
            text("SELECT holiday_date, name, status, updated_at, updated_by FROM hr.holiday")
        ).all()
    assert len(before) == 6  # the five tentative ones of 0004, and one declared

    command.upgrade(alembic_config(), "head")
    with migrated_engine.connect() as conn:
        after = conn.execute(
            text(
                "SELECT holiday_date, name, status, updated_at, updated_by, holiday_id, day_part,"
                " applies_to, created_at, created_by, removed_at FROM hr.holiday"
            )
        ).all()
        pk = conn.execute(
            text(
                "SELECT a.attname FROM pg_index i JOIN pg_attribute a"
                " ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)"
                " WHERE i.indrelid = 'hr.holiday'::regclass AND i.indisprimary"
            )
        ).all()
        scope_rows = conn.execute(text("SELECT count(*) FROM hr.holiday_scope")).scalar_one()
    assert sorted(tuple(r[:5]) for r in after) == sorted(tuple(r) for r in before)
    assert all(r.day_part == "FULL_DAY" and r.applies_to == "ALL" for r in after)
    assert all(r.removed_at is None for r in after)
    assert len({r.holiday_id for r in after}) == 6
    year_end = next(r for r in after if r.name == "Year end")
    assert year_end.created_by == "u-1" and year_end.created_at == year_end.updated_at
    assert [p[0] for p in pk] == ["holiday_id"]
    assert scope_rows == 0

    # two holidays can now share a date when their halves or scopes differ
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hr.holiday (holiday_date, name, status, day_part)"
                " VALUES ('2026-12-30', 'Eve', 'DECLARED', 'MORNING'),"
                " ('2026-12-30', 'Eve 2', 'DECLARED', 'AFTERNOON')"
            )
        )
    # but not two company-wide holidays for the same half
    with pytest.raises(Exception, match="holiday_company_part_ux"):
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO hr.holiday (holiday_date, name, status)"
                    " VALUES ('2026-12-31', 'Again', 'DECLARED')"
                )
            )
    with migrated_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM hr.holiday WHERE holiday_date IN ('2026-12-30', '2026-12-31')")
        )


def test_downgrading_past_the_holiday_migration_is_refused(migrated_engine):
    # Migrations here are forward-only (README): there is no way back to the one-row-per-date table.
    with pytest.raises(RuntimeError, match="forward-only"):
        command.downgrade(alembic_config(), "0024_attendance_correction")
