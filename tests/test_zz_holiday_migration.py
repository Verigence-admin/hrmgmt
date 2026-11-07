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


def test_birthday_leave_grants_above_the_quota_are_corrected_once(migrated_engine):
    """Migration 0027: a SPECIAL grant above leave.special_days_per_year gets one ADJUST down to it,
    unless more was used, or that birthday came before joining (the joining rule wins)."""
    with migrated_engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS hr CASCADE"))
    command.upgrade(alembic_config(), "0026_working_sunday")
    people = {
        # name: (date of birth, joined, granted, used)
        "fix": ("1990-08-30", "2025-01-01", 2, 0),
        "used_one": ("1990-03-01", "2025-01-01", 2, 1),
        "used_two": ("1990-03-01", "2025-01-01", 2, 2),
        "joined_after": ("1990-08-30", "2026-10-13", 2, 0),
        "leap": ("1992-02-29", "2026-02-28", 2, 0),  # 28 Feb 2026 birthday, joined that day
        "right": ("1990-08-30", "2025-01-01", 1, 0),
    }
    ids = {}
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hr.setting (key, value) VALUES"
                " ('leave.special_days_per_year', CAST('1' AS jsonb))"
            )
        )
        for name, (dob, joined, granted, used) in people.items():
            eid = conn.execute(
                text(
                    "INSERT INTO hr.employee (employee_code, full_name, personal_email, created_by,"
                    " updated_by, date_of_birth, date_of_joining) VALUES (:c, :n, :m, 't', 't',"
                    " :b, :j) RETURNING employee_id"
                ),
                {"c": name.upper(), "n": name, "m": f"{name}@example.com", "b": dob, "j": joined},
            ).scalar_one()
            ids[name] = str(eid)
            conn.execute(
                text(
                    "INSERT INTO hr.leave_ledger (employee_id, leave_type, leave_year, entry_type,"
                    " days, note) VALUES (:e, 'SPECIAL', 2026, 'GRANT', :g, 'Yearly quota')"
                ),
                {"e": eid, "g": granted},
            )
            if used:
                conn.execute(
                    text(
                        "INSERT INTO hr.leave_ledger (employee_id, leave_type, leave_year,"
                        " entry_type, days) VALUES (:e, 'SPECIAL', 2026, 'DEDUCT', :d)"
                    ),
                    {"e": eid, "d": -used},
                )
    command.upgrade(alembic_config(), "head")
    command.stamp(alembic_config(), "0026_working_sunday")
    command.upgrade(alembic_config(), "head")  # run again: nothing more is added

    def corrections(name):
        with migrated_engine.connect() as conn:
            return [
                (float(d), n)
                for d, n in conn.execute(
                    text(
                        "SELECT days, note FROM hr.leave_ledger WHERE employee_id = CAST(:e AS uuid)"
                        " AND entry_type = 'ADJUST'"
                    ),
                    {"e": ids[name]},
                )
            ]

    fixed = corrections("fix")
    assert len(fixed) == 1 and fixed[0][0] == -1.0
    assert fixed[0][1].startswith("Quota correction: birthday leave is 1 day(s) per birthday")
    assert len(corrections("used_one")) == 1
    assert corrections("used_two") == []
    assert corrections("joined_after") == []
    assert len(corrections("leap")) == 1
    assert corrections("right") == []
    with migrated_engine.connect() as conn:
        grants = conn.execute(
            text("SELECT count(*) FROM hr.leave_ledger WHERE entry_type = 'GRANT'")
        ).scalar_one()
    assert grants == len(people)  # nothing deleted
    with migrated_engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS hr CASCADE"))
    command.upgrade(alembic_config(), "head")
