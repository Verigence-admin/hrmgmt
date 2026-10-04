"""Messages go to Verigence users, whether or not they are employees.

Revision ID: 0009_message_recipients
Revises: 0008_messages
"""

from alembic import op

revision = "0009_message_recipients"
down_revision = "0008_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE hr.message_log ALTER COLUMN employee_id DROP NOT NULL")
    op.execute("ALTER TABLE hr.message_log ADD COLUMN user_id uuid")
    op.execute("ALTER TABLE hr.message_log ADD COLUMN recipient_name text")
    op.execute("CREATE INDEX message_log_user_ix ON hr.message_log (user_id, sent_at DESC)")


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
