"""Previous employment: company, designation and period, several per employee.

Revision ID: 0012_employee_experience
Revises: 0011_designations
"""

from alembic import op

revision = "0012_employee_experience"
down_revision = "0011_designations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE hr.employee_experience (
            experience_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            employee_id   uuid NOT NULL REFERENCES hr.employee (employee_id),
            company       text NOT NULL,
            location      text,
            designation   text NOT NULL,
            from_date     date NOT NULL,
            to_date       date NOT NULL,
            description   text,
            created_at    timestamptz NOT NULL DEFAULT now(),
            created_by    text NOT NULL,
            updated_at    timestamptz NOT NULL DEFAULT now(),
            updated_by    text NOT NULL,
            CONSTRAINT experience_period CHECK (to_date >= from_date)
        )
        """
    )
    op.execute(
        "CREATE INDEX employee_experience_employee_idx ON hr.employee_experience (employee_id)"
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
