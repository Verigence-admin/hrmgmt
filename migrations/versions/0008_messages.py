"""Message templates and a log of what was sent, by channel (email now; others later).

Revision ID: 0008_messages
Revises: 0007_payroll
"""

from alembic import op

revision = "0008_messages"
down_revision = "0007_payroll"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE hr.message_template (
            code       text PRIMARY KEY,
            channel    text NOT NULL DEFAULT 'EMAIL',
            name       text NOT NULL,
            subject    text NOT NULL,
            body       text NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            updated_by text
        )
        """
    )
    # The message that went out is not kept; the password never is. Only who, which template,
    # when, and whether it worked.
    op.execute(
        """
        CREATE TABLE hr.message_log (
            log_id        bigserial PRIMARY KEY,
            employee_id   uuid NOT NULL REFERENCES hr.employee (employee_id),
            channel       text NOT NULL DEFAULT 'EMAIL',
            template_code text NOT NULL,
            status        text NOT NULL CHECK (status IN ('SENT', 'FAILED', 'SKIPPED')),
            reason_code   text,
            sent_by       text NOT NULL,
            sent_at       timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX message_log_employee_ix ON hr.message_log (employee_id, sent_at DESC)")
    op.execute("CREATE INDEX message_log_recent_ix ON hr.message_log (sent_at DESC)")


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
