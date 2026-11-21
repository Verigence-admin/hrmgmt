"""Caution days (product owner, 21 Nov 2026).

On a day of bad weather, a cyclone or a strike the team is offered voluntary leave and asked to work
the coming Sunday instead. HR or SuperAdmin declares the caution day with a reason: a row of
hr.holiday with kind CAUTION (a full day, declared, not a Sunday), for the whole company or with
scope rows like any holiday, and with it a WORKING_DAY row for the Sunday whose caution_id points at
the caution day. Whoever checks in on the caution day keeps that Sunday off (in lieu); see
hrmgmt.holidays.

Revision ID: 0030_caution_day
Revises: 0029_attendance_adjustment
"""

from alembic import op

revision = "0030_caution_day"
down_revision = "0029_attendance_adjustment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Safe to run again (the migration tests replay every revision after 0026).
    op.execute("ALTER TABLE hr.holiday DROP CONSTRAINT IF EXISTS holiday_kind_check")
    op.execute(
        "ALTER TABLE hr.holiday ADD CONSTRAINT holiday_kind_check"
        " CHECK (kind IN ('HOLIDAY', 'WORKING_DAY', 'CAUTION'))"
    )
    op.execute("ALTER TABLE hr.holiday DROP CONSTRAINT IF EXISTS holiday_working_day_shape")
    op.execute(
        "ALTER TABLE hr.holiday ADD CONSTRAINT holiday_working_day_shape CHECK (kind <> 'WORKING_DAY'"
        " OR (day_part = 'FULL_DAY' AND status = 'DECLARED' AND extract(isodow FROM holiday_date) = 7))"
    )
    op.execute("ALTER TABLE hr.holiday DROP CONSTRAINT IF EXISTS holiday_caution_shape")
    op.execute("ALTER TABLE hr.holiday DROP CONSTRAINT IF EXISTS holiday_caution_link")
    # A caution day is a whole weekday, declared (it is decided on the day, never a proposal).
    op.execute(
        "ALTER TABLE hr.holiday ADD CONSTRAINT holiday_caution_shape CHECK (kind <> 'CAUTION'"
        " OR (day_part = 'FULL_DAY' AND status = 'DECLARED' AND extract(isodow FROM holiday_date) <> 7))"
    )
    op.execute(
        "ALTER TABLE hr.holiday ADD COLUMN IF NOT EXISTS caution_id uuid REFERENCES hr.holiday (holiday_id)"
    )
    # Only a working Sunday stands in for a caution day.
    op.execute(
        "ALTER TABLE hr.holiday ADD CONSTRAINT holiday_caution_link"
        " CHECK (caution_id IS NULL OR kind = 'WORKING_DAY')"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS holiday_caution_ix ON hr.holiday (caution_id) WHERE caution_id IS NOT NULL"
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
