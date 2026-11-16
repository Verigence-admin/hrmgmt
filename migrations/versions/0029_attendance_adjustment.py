"""Correcting a check-in or check-out time, or a location, and the month's attendance limits
(product owner, 16 Nov 2026).

An employee may ask for the check-in time, the check-out time or the location of a day to be
corrected (requested = 'ADJUST'). Once the Team Lead or Project Manager approves it, the new time
is written to the day; what was recorded before is kept on the correction. A late, early,
out-of-fence or short-day exception that the correction answers becomes RESOLVED.

A day may have one waiting correction at a time, and one approved correction of what it counts
(FULL_DAY or HALF_DAY). Approved time corrections may follow one another.

hr.attendance_escalation records, once per person per month, that the person passed a monthly
limit (late and early hours, or average hours a day) and was told so.

Revision ID: 0029_attendance_adjustment
Revises: 0028_check_out_correction
"""

from alembic import op

revision = "0029_attendance_adjustment"
down_revision = "0028_check_out_correction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        "check_in_at timestamptz",
        "location_check_in boolean NOT NULL DEFAULT false",
        "location_check_out boolean NOT NULL DEFAULT false",
        "previous_check_in_at timestamptz",
        "previous_check_out_at timestamptz",
    ):
        op.execute(f"ALTER TABLE hr.attendance_correction ADD COLUMN IF NOT EXISTS {column}")
    op.execute(
        "ALTER TABLE hr.attendance_correction"
        " DROP CONSTRAINT IF EXISTS attendance_correction_requested_check"
    )
    op.execute(
        "ALTER TABLE hr.attendance_correction ADD CONSTRAINT attendance_correction_requested_check"
        " CHECK (requested IN ('FULL_DAY', 'HALF_DAY', 'CHECK_OUT', 'ADJUST'))"
    )
    op.execute(
        "ALTER TABLE hr.attendance_correction"
        " DROP CONSTRAINT IF EXISTS attendance_correction_check_out_time"
    )
    op.execute(
        "ALTER TABLE hr.attendance_correction DROP CONSTRAINT IF EXISTS attendance_correction_parts"
    )
    op.execute(
        """
        ALTER TABLE hr.attendance_correction ADD CONSTRAINT attendance_correction_parts CHECK (
            (requested IN ('FULL_DAY', 'HALF_DAY') AND check_in_at IS NULL
                AND check_out_at IS NULL AND NOT location_check_in AND NOT location_check_out)
            OR (requested = 'CHECK_OUT' AND check_out_at IS NOT NULL AND check_in_at IS NULL
                AND NOT location_check_in AND NOT location_check_out)
            OR (requested = 'ADJUST' AND (check_in_at IS NOT NULL OR check_out_at IS NOT NULL
                OR location_check_in OR location_check_out))
        )
        """
    )
    op.execute("DROP INDEX IF EXISTS hr.attendance_correction_live_ux")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS attendance_correction_waiting_ux"
        " ON hr.attendance_correction (employee_id, work_date) WHERE status = 'PENDING'"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS attendance_correction_day_count_ux"
        " ON hr.attendance_correction (employee_id, work_date)"
        " WHERE status = 'APPROVED' AND requested IN ('FULL_DAY', 'HALF_DAY')"
    )

    op.execute(
        "ALTER TABLE hr.attendance_exception DROP CONSTRAINT IF EXISTS attendance_exception_status_check"
    )
    op.execute(
        "ALTER TABLE hr.attendance_exception ADD CONSTRAINT attendance_exception_status_check"
        " CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'RESOLVED'))"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS hr.attendance_escalation (
            employee_id  uuid NOT NULL REFERENCES hr.employee (employee_id),
            month        date NOT NULL CHECK (extract(day FROM month) = 1),
            reasons      jsonb NOT NULL,
            created_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (employee_id, month)
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
