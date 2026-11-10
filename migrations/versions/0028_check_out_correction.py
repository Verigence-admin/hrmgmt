"""A correction can ask for a missed check-out to be recorded (product owner, 10 Nov 2026).

An employee who checked in but could not check out (the app failed, the phone died, they forgot)
asks for their check-out to be recorded at the time they left, with a reason. It goes to the Team
Lead or Project Manager like any correction; once approved, the check-out is written to the day at
that time (marked as recorded from an approved correction) and the day counts as any other.

Revision ID: 0028_check_out_correction
Revises: 0027_birthday_quota_correction
"""

from alembic import op

revision = "0028_check_out_correction"
down_revision = "0027_birthday_quota_correction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE hr.attendance_correction ADD COLUMN IF NOT EXISTS check_out_at timestamptz"
    )
    op.execute(
        "ALTER TABLE hr.attendance_correction"
        " DROP CONSTRAINT IF EXISTS attendance_correction_requested_check"
    )
    op.execute(
        "ALTER TABLE hr.attendance_correction ADD CONSTRAINT attendance_correction_requested_check"
        " CHECK (requested IN ('FULL_DAY', 'HALF_DAY', 'CHECK_OUT'))"
    )
    op.execute(
        "ALTER TABLE hr.attendance_correction"
        " DROP CONSTRAINT IF EXISTS attendance_correction_check_out_time"
    )
    op.execute(
        "ALTER TABLE hr.attendance_correction ADD CONSTRAINT attendance_correction_check_out_time"
        " CHECK ((requested = 'CHECK_OUT') = (check_out_at IS NOT NULL))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE hr.attendance_correction"
        " DROP CONSTRAINT IF EXISTS attendance_correction_check_out_time"
    )
    op.execute("DELETE FROM hr.attendance_correction WHERE requested = 'CHECK_OUT'")
    op.execute(
        "ALTER TABLE hr.attendance_correction"
        " DROP CONSTRAINT IF EXISTS attendance_correction_requested_check"
    )
    op.execute(
        "ALTER TABLE hr.attendance_correction ADD CONSTRAINT attendance_correction_requested_check"
        " CHECK (requested IN ('FULL_DAY', 'HALF_DAY'))"
    )
    op.execute("ALTER TABLE hr.attendance_correction DROP COLUMN IF EXISTS check_out_at")
