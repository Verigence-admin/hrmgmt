"""Employee status becomes Active, Suspended, Terminated or Quit, and a change needs approval.

HR asks for a change and the CEO approves it (the status changes only then). The old values map as
INACTIVE -> SUSPENDED and EXITED -> TERMINATED; nobody can be left with an unknown status.

Revision ID: 0018_employee_status
Revises: 0017_off_day_work
"""

from alembic import op

revision = "0018_employee_status"
down_revision = "0017_off_day_work"
branch_labels = None
depends_on = None

_STATUSES = "('ACTIVE', 'SUSPENDED', 'TERMINATED', 'QUIT')"


def upgrade() -> None:
    op.execute("ALTER TABLE hr.employee DROP CONSTRAINT employee_employment_status_check")
    op.execute("UPDATE hr.employee SET employment_status = 'SUSPENDED' WHERE employment_status = 'INACTIVE'")
    op.execute("UPDATE hr.employee SET employment_status = 'TERMINATED' WHERE employment_status = 'EXITED'")
    op.execute(
        "ALTER TABLE hr.employee ADD CONSTRAINT employee_employment_status_check"
        f" CHECK (employment_status IN {_STATUSES})"
    )
    op.execute(
        f"""
        CREATE TABLE hr.employee_status_change (
            change_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            employee_id    uuid NOT NULL REFERENCES hr.employee (employee_id),
            from_status    text NOT NULL CHECK (from_status IN {_STATUSES}),
            to_status      text NOT NULL CHECK (to_status IN {_STATUSES}),
            effective_date date NOT NULL,
            reason         text NOT NULL,
            status         text NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')),
            requested_by   text NOT NULL,
            requested_at   timestamptz NOT NULL DEFAULT now(),
            decided_by     text,
            decided_at     timestamptz,
            decision_note  text,
            -- What happened to the Verigence login when the change was approved.
            login_outcome  text,
            CHECK (from_status <> to_status)
        )
        """
    )
    # One open request per employee: a second one must wait for the first to be decided.
    op.execute(
        "CREATE UNIQUE INDEX employee_status_change_open_uq"
        " ON hr.employee_status_change (employee_id) WHERE status = 'PENDING'"
    )
    op.execute(
        "CREATE INDEX employee_status_change_ix ON hr.employee_status_change (status, requested_at DESC)"
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
