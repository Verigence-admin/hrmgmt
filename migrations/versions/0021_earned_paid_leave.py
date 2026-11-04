"""Paid leave is earned from attendance, not granted each year.

Product owner rule (4 Nov 2026): one paid leave is earned on completing 22 days of attendance
(both numbers are settings), up to a cap on the balance (a setting, 3 to start). There is no sick
leave. A Special occasion leave has its own yearly quota, and an approved day worked on a Sunday or
holiday earns a comp-off day.

- leave types PAID, SPECIAL and COMP_OFF are added; SICK and EARNED stay only as history;
- the ledger gains EARN entries, and a comp-off credit names the exception that earned it, so a day
  can never be credited twice;
- hr.leave_accrual records every completed block of attendance days and what it earned, so a
  balance can always be traced back to the days that earned it;
- the yearly Sick and Earned days still unused are withdrawn by an ADJUST entry (append-only: the
  grant stays, the withdrawal is a new row), and a pending Sick or Earned request becomes PAID.

Revision ID: 0021_earned_paid_leave
Revises: 0020_contact_change_request
"""

from alembic import op

revision = "0021_earned_paid_leave"
down_revision = "0020_contact_change_request"
branch_labels = None
depends_on = None

_WITHDRAW_NOTE = "Yearly allowance withdrawn: paid leave is now earned from attendance"


def upgrade() -> None:
    op.execute("ALTER TABLE hr.leave_request DROP CONSTRAINT leave_request_leave_type_check")
    op.execute(
        "ALTER TABLE hr.leave_request ADD CONSTRAINT leave_request_leave_type_check CHECK"
        " (leave_type IN ('PAID', 'SPECIAL', 'COMP_OFF', 'UNPAID', 'SICK', 'EARNED'))"
    )
    op.execute("ALTER TABLE hr.leave_request ADD COLUMN occasion text")

    op.execute("ALTER TABLE hr.leave_ledger DROP CONSTRAINT leave_ledger_leave_type_check")
    op.execute(
        "ALTER TABLE hr.leave_ledger ADD CONSTRAINT leave_ledger_leave_type_check CHECK"
        " (leave_type IN ('PAID', 'SPECIAL', 'COMP_OFF', 'SICK', 'EARNED'))"
    )
    op.execute("ALTER TABLE hr.leave_ledger DROP CONSTRAINT leave_ledger_entry_type_check")
    op.execute(
        "ALTER TABLE hr.leave_ledger ADD CONSTRAINT leave_ledger_entry_type_check CHECK"
        " (entry_type IN ('GRANT', 'DEDUCT', 'ADJUST', 'REVERSAL', 'EARN'))"
    )
    op.execute("ALTER TABLE hr.leave_ledger ADD COLUMN exception_id uuid")
    op.execute(
        "CREATE UNIQUE INDEX leave_ledger_comp_off_uq ON hr.leave_ledger (exception_id)"
        " WHERE entry_type = 'EARN' AND leave_type = 'COMP_OFF'"
    )

    op.execute(
        """
        CREATE TABLE hr.leave_accrual (
            employee_id   uuid NOT NULL REFERENCES hr.employee (employee_id) ON DELETE CASCADE,
            block_no      integer NOT NULL CHECK (block_no > 0),
            through_date  date NOT NULL,
            present_days  numeric(5, 1) NOT NULL,
            credited      numeric(4, 1) NOT NULL CHECK (credited >= 0),
            lapsed        numeric(4, 1) NOT NULL CHECK (lapsed >= 0),
            carried       numeric(4, 1) NOT NULL CHECK (carried >= 0),
            created_at    timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (employee_id, block_no)
        )
        """
    )

    # The yearly days still unused are withdrawn, per person, per type and year.
    op.execute(
        f"""
        INSERT INTO hr.leave_ledger (employee_id, leave_type, leave_year, entry_type, days, note)
        SELECT employee_id, leave_type, leave_year, 'ADJUST', -sum(days), '{_WITHDRAW_NOTE}'
        FROM hr.leave_ledger
        WHERE leave_type IN ('SICK', 'EARNED')
        GROUP BY employee_id, leave_type, leave_year
        HAVING sum(days) > 0
        """
    )
    op.execute(
        "UPDATE hr.leave_request SET leave_type = 'PAID'"
        " WHERE status = 'PENDING' AND leave_type IN ('SICK', 'EARNED')"
    )
    # The old yearly settings no longer mean anything.
    op.execute(
        "DELETE FROM hr.setting WHERE key IN ('leave.sick_days_per_year', 'leave.earned_days_per_year')"
    )
    # Comp-off is earned for off-day work approved from today on (a new rule, not back-paid).
    op.execute(
        "INSERT INTO hr.setting (key, value) VALUES ('leave.comp_off_from', to_jsonb(current_date::text))"
        " ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
