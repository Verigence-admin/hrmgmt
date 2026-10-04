"""The ₹21,001 to ₹24,999 salary template (no ESI), and PF as a choice above ₹25,000.

Revision ID: 0013_mid_template_pf
Revises: 0012_employee_experience
"""

from alembic import op

revision = "0013_mid_template_pf"
down_revision = "0012_employee_experience"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PF is optional for a gross of ₹25,000 or more: the salary records whether it applies.
    # Everyone else, and every salary already on file, keeps PF applying.
    op.execute(
        "ALTER TABLE hr.salary_structure ADD COLUMN pf_applicable boolean NOT NULL DEFAULT true"
    )
    # ESI does not apply to this band, so no component counts as ESI wage. The split is a
    # placeholder like the other two defaults: HR edits it before first use.
    # exec_driver_sql: the JSON contains colons that op.execute would read as bind parameters.
    op.get_bind().exec_driver_sql(
        """
        INSERT INTO hr.salary_template (code, name, description, components) VALUES
        ('MID_21K_25K', 'Gross ₹21,001 to ₹24,999 (placeholder split)',
         'Template for a monthly gross from ₹21,001 to ₹24,999. ESI does not apply. Edit the split before first use.',
         '[{"code":"BASIC","label":"Basic","basis":"PERCENT_GROSS","value":50,"pf_wage":true,"esi_wage":false},
           {"code":"HRA","label":"House rent allowance","basis":"PERCENT_BASIC","value":40,"pf_wage":false,"esi_wage":false},
           {"code":"OTHER","label":"Other allowance","basis":"REMAINDER","pf_wage":false,"esi_wage":false}]'::jsonb)
        ON CONFLICT (code) DO NOTHING
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
