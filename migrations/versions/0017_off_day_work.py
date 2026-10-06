"""Attendance on a Sunday or a declared holiday is allowed but needs approval: a new exception kind.

Revision ID: 0017_off_day_work
Revises: 0016_leave_ledger_purge
"""

from alembic import op

revision = "0017_off_day_work"
down_revision = "0016_leave_ledger_purge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE hr.attendance_exception DROP CONSTRAINT attendance_exception_kind_check"
    )
    op.execute(
        "ALTER TABLE hr.attendance_exception ADD CONSTRAINT attendance_exception_kind_check"
        " CHECK (kind IN ('LATE_CHECK_IN', 'EARLY_CHECK_OUT', 'OUT_OF_FENCE',"
        " 'NO_OUTLET_LOCATION', 'OFF_DAY_WORK'))"
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
