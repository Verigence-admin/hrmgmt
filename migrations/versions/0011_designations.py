"""The company's designation ladder: Analyst up to Partner.

Revision ID: 0011_designations
Revises: 0010_onboarding_details
"""

from alembic import op

revision = "0011_designations"
down_revision = "0010_onboarding_details"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Auditor and Senior Auditor are retired, not deleted: anyone already given one keeps it
    # on their record until HR picks a new designation, but it is no longer offered.
    op.execute(
        """
        INSERT INTO hr.designation (code, label, sort_order, active) VALUES
            ('ANALYST', 'Analyst', 1, true),
            ('SENIOR_ANALYST', 'Senior Analyst', 2, true),
            ('CONSULTANT', 'Consultant', 3, true),
            ('SENIOR_CONSULTANT', 'Senior Consultant', 4, true),
            ('ASSISTANT_MANAGER', 'Assistant Manager', 5, true),
            ('MANAGER', 'Manager', 6, true),
            ('SENIOR_MANAGER', 'Senior Manager', 7, true),
            ('DIRECTOR', 'Director', 8, true),
            ('PARTNER', 'Partner', 9, true)
        ON CONFLICT (code) DO UPDATE
            SET label = EXCLUDED.label, sort_order = EXCLUDED.sort_order, active = true
        """
    )
    op.execute(
        "UPDATE hr.designation SET active = false, sort_order = 100 + sort_order"
        " WHERE code IN ('AUDITOR', 'SENIOR_AUDITOR')"
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
