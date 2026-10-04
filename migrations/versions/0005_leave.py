"""Leave: requests, and a ledger for balances.

Revision ID: 0005_leave
Revises: 0004_attendance
"""

from alembic import op

revision = "0005_leave"
down_revision = "0004_attendance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A balance is never an editable number: it is the sum of ledger rows. Grants and
    # adjustments add days, an approved leave subtracts them, a reversal adds them back.
    op.execute(
        """
        CREATE TABLE hr.leave_request (
            request_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            employee_id   uuid NOT NULL REFERENCES hr.employee (employee_id),
            leave_type    text NOT NULL CHECK (leave_type IN ('SICK', 'EARNED', 'UNPAID')),
            from_date     date NOT NULL,
            to_date       date NOT NULL,
            half_day      boolean NOT NULL DEFAULT false,
            days          numeric(4, 1) NOT NULL CHECK (days > 0),
            reason        text,
            status        text NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')),
            approver_rule text NOT NULL
                CHECK (approver_rule IN ('AUTO', 'TL_PM', 'PM', 'CEO', 'HR')),
            submitted_at  timestamptz NOT NULL DEFAULT now(),
            decided_by    text,
            decided_at    timestamptz,
            decision_note text,
            CHECK (to_date >= from_date),
            CHECK (NOT half_day OR from_date = to_date),
            CHECK (EXTRACT(year FROM from_date) = EXTRACT(year FROM to_date))
        )
        """
    )
    op.execute(
        "CREATE INDEX leave_request_employee_ix ON hr.leave_request (employee_id, from_date)"
    )
    op.execute("CREATE INDEX leave_request_status_ix ON hr.leave_request (status, from_date)")
    op.execute(
        """
        CREATE TABLE hr.leave_ledger (
            ledger_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            employee_id uuid NOT NULL REFERENCES hr.employee (employee_id),
            leave_type  text NOT NULL CHECK (leave_type IN ('SICK', 'EARNED')),
            leave_year  integer NOT NULL,
            entry_type  text NOT NULL CHECK (entry_type IN ('GRANT', 'DEDUCT', 'ADJUST', 'REVERSAL')),
            days        numeric(5, 1) NOT NULL CHECK (days <> 0),
            request_id  uuid REFERENCES hr.leave_request (request_id),
            note        text,
            created_at  timestamptz NOT NULL DEFAULT now(),
            created_by  text
        )
        """
    )
    op.execute(
        "CREATE INDEX leave_ledger_balance_ix ON hr.leave_ledger (employee_id, leave_year, leave_type)"
    )
    # One yearly grant per person, type and year: concurrent first reads cannot grant twice.
    op.execute(
        "CREATE UNIQUE INDEX leave_ledger_grant_uq ON hr.leave_ledger"
        " (employee_id, leave_year, leave_type) WHERE entry_type = 'GRANT'"
    )
    # The ledger only grows: a mistake is corrected by a new row, never by editing history.
    op.execute(
        """
        CREATE FUNCTION hr.leave_ledger_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'hr.leave_ledger is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER leave_ledger_no_change BEFORE UPDATE OR DELETE ON hr.leave_ledger"
        " FOR EACH ROW EXECUTE FUNCTION hr.leave_ledger_append_only()"
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
