"""Housekeeping may clear leave records: the append-only ledger allows a delete only inside a
purge, which says so for its own transaction. Every other delete or update still fails.

Revision ID: 0016_leave_ledger_purge
Revises: 0015_support_tickets
"""

from alembic import op

revision = "0016_leave_ledger_purge"
down_revision = "0015_support_tickets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION hr.leave_ledger_append_only() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND current_setting('hr.allow_ledger_purge', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'hr.leave_ledger is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
