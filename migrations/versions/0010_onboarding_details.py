"""District on the employee; university and college on each qualification.

Revision ID: 0010_onboarding_details
Revises: 0009_message_recipients
"""

from alembic import op

revision = "0010_onboarding_details"
down_revision = "0009_message_recipients"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: people entered before these fields existed keep their record. What is missing
    # is shown to HR as a "Pending details" note, computed from the data, never typed in.
    op.execute("ALTER TABLE hr.employee ADD COLUMN district text")
    op.execute("ALTER TABLE hr.employee_qualification ADD COLUMN university text")
    op.execute("ALTER TABLE hr.employee_qualification ADD COLUMN college text")


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
