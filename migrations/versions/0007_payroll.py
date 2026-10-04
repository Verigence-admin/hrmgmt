"""Salary templates and structures, statutory configuration, payroll runs and payslips.

Revision ID: 0007_payroll
Revises: 0006_claims
"""

from alembic import op

revision = "0007_payroll"
down_revision = "0006_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Templates are generic starting points that HR edits. The split below is a placeholder,
    # not advice: it exists so a salary can be structured today and refined later.
    op.execute(
        """
        CREATE TABLE hr.salary_template (
            template_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            code        text NOT NULL UNIQUE,
            name        text NOT NULL,
            description text,
            components  jsonb NOT NULL,
            active      boolean NOT NULL DEFAULT true,
            updated_at  timestamptz NOT NULL DEFAULT now(),
            updated_by  text
        )
        """
    )
    # exec_driver_sql: the JSON below contains colons that op.execute would read as bind parameters.
    op.get_bind().exec_driver_sql(
        """
        INSERT INTO hr.salary_template (code, name, description, components) VALUES
        ('BELOW_21K', 'Gross up to ₹21,000 (placeholder split)',
         'Generic template for a monthly gross of ₹21,000 or less. Edit the split before first use.',
         '[{"code":"BASIC","label":"Basic","basis":"PERCENT_GROSS","value":50,"pf_wage":true,"esi_wage":true},
           {"code":"HRA","label":"House rent allowance","basis":"PERCENT_BASIC","value":40,"pf_wage":false,"esi_wage":true},
           {"code":"OTHER","label":"Other allowance","basis":"REMAINDER","pf_wage":false,"esi_wage":true}]'::jsonb),
        ('ABOVE_25K', 'Gross above ₹25,000 (placeholder split)',
         'Generic template for a monthly gross above ₹25,000. Edit the split before first use.',
         '[{"code":"BASIC","label":"Basic","basis":"PERCENT_GROSS","value":40,"pf_wage":true,"esi_wage":true},
           {"code":"HRA","label":"House rent allowance","basis":"PERCENT_BASIC","value":50,"pf_wage":false,"esi_wage":true},
           {"code":"SPECIAL","label":"Special allowance","basis":"REMAINDER","pf_wage":false,"esi_wage":true}]'::jsonb)
        """
    )
    # A salary for one person, effective from a date. HR proposes, Finance approves; every version
    # stays. `components` holds the rupee amounts worked out when it was proposed.
    op.execute(
        """
        CREATE TABLE hr.salary_structure (
            structure_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            employee_id    uuid NOT NULL REFERENCES hr.employee (employee_id),
            template_id    uuid REFERENCES hr.salary_template (template_id),
            gross_monthly  numeric(12, 2) NOT NULL CHECK (gross_monthly > 0),
            components     jsonb NOT NULL,
            effective_from date NOT NULL,
            status         text NOT NULL DEFAULT 'PROPOSED'
                CHECK (status IN ('PROPOSED', 'APPROVED', 'REJECTED', 'SUPERSEDED')),
            note           text,
            proposed_by    text NOT NULL,
            proposed_at    timestamptz NOT NULL DEFAULT now(),
            decided_by     text,
            decided_at     timestamptz,
            decision_note  text
        )
        """
    )
    op.execute(
        "CREATE INDEX salary_structure_employee_ix ON hr.salary_structure (employee_id, effective_from)"
    )
    op.execute("CREATE INDEX salary_structure_status_ix ON hr.salary_structure (status)")

    # One row. HR enters the rates and slabs; a run cannot be approved until the CA's
    # confirmation is recorded here.
    op.execute(
        """
        CREATE TABLE hr.statutory_config (
            singleton      boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            config         jsonb NOT NULL,
            updated_at     timestamptz NOT NULL DEFAULT now(),
            updated_by     text,
            confirmed_at   timestamptz,
            confirmed_by   text,
            confirmation_note text
        )
        """
    )
    op.get_bind().exec_driver_sql(
        """
        INSERT INTO hr.statutory_config (singleton, config) VALUES (true,
          '{"pf":{"enabled":false,"rounding":"NEAREST"},"esi":{"enabled":false,"rounding":"NEAREST"},"pt":{"enabled":false,"state":null,"slabs":[]}}'::jsonb)
        """
    )

    op.execute(
        """
        CREATE TABLE hr.payroll_run (
            run_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            pay_month          date NOT NULL CHECK (pay_month = date_trunc('month', pay_month)::date),
            status             text NOT NULL DEFAULT 'DRAFT' CHECK (status IN
                ('DRAFT', 'SUBMITTED', 'APPROVED', 'PAID', 'CANCELLED')),
            statutory_config   jsonb NOT NULL,
            statutory_confirmed boolean NOT NULL DEFAULT false,
            skipped            jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_by         text NOT NULL,
            created_at         timestamptz NOT NULL DEFAULT now(),
            submitted_by       text,
            submitted_at       timestamptz,
            approved_by        text,
            approved_at        timestamptz,
            paid_at            timestamptz,
            payment_date       date,
            note               text
        )
        """
    )
    # One live run per month: a mistake is fixed in the next month's adjustments, not by a second run.
    op.execute(
        "CREATE UNIQUE INDEX payroll_run_month_uq ON hr.payroll_run (pay_month) WHERE status <> 'CANCELLED'"
    )
    op.execute(
        """
        CREATE TABLE hr.payroll_line (
            line_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id         uuid NOT NULL REFERENCES hr.payroll_run (run_id),
            employee_id    uuid NOT NULL REFERENCES hr.employee (employee_id),
            structure_id   uuid NOT NULL REFERENCES hr.salary_structure (structure_id),
            employee_code  text NOT NULL,
            employee_name  text NOT NULL,
            designation    text,
            -- what HR types in; kept when the line is recalculated
            extra_lop_days numeric(4, 1) NOT NULL DEFAULT 0 CHECK (extra_lop_days >= 0),
            adjustments    jsonb NOT NULL DEFAULT '[]'::jsonb,
            -- the worked-out month
            figures        jsonb NOT NULL,
            net_pay        numeric(12, 2) NOT NULL,
            payable_total  numeric(12, 2) NOT NULL,
            CONSTRAINT payroll_line_one_per_person_uq UNIQUE (run_id, employee_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE hr.payslip (
            payslip_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id      uuid NOT NULL REFERENCES hr.payroll_run (run_id),
            employee_id uuid NOT NULL REFERENCES hr.employee (employee_id),
            pay_month   date NOT NULL,
            file_key    text NOT NULL,
            issued_at   timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT payslip_one_per_run_uq UNIQUE (run_id, employee_id)
        )
        """
    )
    op.execute("CREATE INDEX payslip_employee_ix ON hr.payslip (employee_id, pay_month)")
    op.execute(
        "ALTER TABLE hr.claim ADD CONSTRAINT claim_payroll_run_fk FOREIGN KEY (payroll_run_id)"
        " REFERENCES hr.payroll_run (run_id)"
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
