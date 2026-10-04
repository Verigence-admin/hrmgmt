"""Clear every employee's department; it is now chosen from Audit, Finance, HR, IT or CRM.

Revision ID: 0014_clear_departments
Revises: 0013_mid_template_pf
"""

from alembic import op

revision = "0014_clear_departments"
down_revision = "0013_mid_template_pf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The old values were free text from the first import. HR sets each person's department from
    # the fixed list; until then the field is empty. One audit row records how many were cleared.
    op.execute(
        """
        WITH cleared AS (
            UPDATE hr.employee SET department = NULL, updated_at = now()
            WHERE department IS NOT NULL
            RETURNING 1
        )
        INSERT INTO hr.audit_log (actor_user_id, action, entity_type, entity_id, changes)
        SELECT 'system', 'DEPARTMENTS_CLEARED', 'employee', 'all',
               jsonb_build_object('cleared', (SELECT count(*) FROM cleared))
        WHERE EXISTS (SELECT 1 FROM cleared)
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
