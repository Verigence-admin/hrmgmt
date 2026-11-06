"""A day shorter than the minimum hours goes to the Team Lead (product owner, 6 Nov 2026).

At check-out, a working day with less than `attendance.min_work_hours` (5 to start) between
check-in and check-out becomes a SHORT_DAY exception: the person gives a reason, the Team Lead
approves or rejects it, and the project's Project Managers are told by email.

Revision ID: 0023_short_day
Revises: 0022_approver_pause
"""

from alembic import op

revision = "0023_short_day"
down_revision = "0022_approver_pause"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE hr.attendance_exception DROP CONSTRAINT attendance_exception_kind_check"
    )
    op.execute(
        "ALTER TABLE hr.attendance_exception ADD CONSTRAINT attendance_exception_kind_check"
        " CHECK (kind IN ('LATE_CHECK_IN', 'EARLY_CHECK_OUT', 'OUT_OF_FENCE',"
        " 'NO_OUTLET_LOCATION', 'OFF_DAY_WORK', 'SHORT_DAY'))"
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
