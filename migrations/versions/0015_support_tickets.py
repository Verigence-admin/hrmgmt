"""Feedback & Support: tickets raised by employees, a two-way conversation and attached files.

Revision ID: 0015_support_tickets
Revises: 0014_clear_departments
"""

from alembic import op

revision = "0015_support_tickets"
down_revision = "0014_clear_departments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE hr.ticket (
            ticket_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            ticket_no      bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            employee_id    uuid NOT NULL REFERENCES hr.employee (employee_id),
            employee_code  text NOT NULL,
            employee_name  text NOT NULL,
            employee_email text NOT NULL,
            summary        text NOT NULL,
            page_path      text,
            status         text NOT NULL DEFAULT 'OPEN'
                           CHECK (status IN ('OPEN', 'IN_PROGRESS', 'CLOSED')),
            admin_note     text,
            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now(),
            updated_by     text
        )
        """
    )
    op.execute("CREATE INDEX ticket_created_idx ON hr.ticket (created_at DESC)")
    op.execute("CREATE INDEX ticket_employee_idx ON hr.ticket (employee_id, created_at DESC)")
    op.execute(
        """
        CREATE TABLE hr.ticket_message (
            message_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            ticket_id      uuid NOT NULL REFERENCES hr.ticket (ticket_id),
            author_user_id text NOT NULL,
            author_kind    text NOT NULL CHECK (author_kind IN ('EMPLOYEE', 'SUPPORT')),
            author_name    text NOT NULL,
            body           text NOT NULL,
            created_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ticket_message_ticket_idx ON hr.ticket_message (ticket_id, created_at)"
    )
    op.execute(
        """
        CREATE TABLE hr.ticket_file (
            file_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            ticket_id    uuid NOT NULL REFERENCES hr.ticket (ticket_id),
            message_id   uuid NOT NULL REFERENCES hr.ticket_message (message_id),
            file_key     text NOT NULL,
            file_name    text NOT NULL,
            content_type text NOT NULL,
            size_bytes   integer NOT NULL,
            created_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ticket_file_ticket_idx ON hr.ticket_file (ticket_id)")


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
