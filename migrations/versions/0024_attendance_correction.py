"""Attendance correction requests (product owner, 6 Nov 2026).

An employee asks for a past working day to count as a full day or a half day (forgot to check in
or out, the app failed, a rejected exception). The Team Lead or Project Manager decides, like any
attendance exception. An approved correction sets what the day counts in payroll and earned leave.
At most one live (pending or approved) correction per person per day.

Revision ID: 0024_attendance_correction
Revises: 0023_short_day
"""

from alembic import op

revision = "0024_attendance_correction"
down_revision = "0023_short_day"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE hr.attendance_correction (
            correction_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            employee_id    uuid NOT NULL REFERENCES hr.employee (employee_id),
            work_date      date NOT NULL,
            requested      text NOT NULL CHECK (requested IN ('FULL_DAY', 'HALF_DAY')),
            reason         text NOT NULL CHECK (length(btrim(reason)) > 0),
            status         text NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')),
            decided_by     text,
            decided_at     timestamptz,
            decision_note  text,
            created_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX attendance_correction_live_ux ON hr.attendance_correction"
        " (employee_id, work_date) WHERE status IN ('PENDING', 'APPROVED')"
    )
    op.execute(
        "CREATE INDEX attendance_correction_status_ix ON hr.attendance_correction"
        " (status, work_date)"
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
