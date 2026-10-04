"""Reimbursement claims: categories, claims, receipts and their history.

Revision ID: 0006_claims
Revises: 0005_leave
"""

from alembic import op

revision = "0006_claims"
down_revision = "0005_leave"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE hr.claim_category (
            category_code    text PRIMARY KEY,
            label            text NOT NULL,
            kind             text NOT NULL CHECK (kind IN ('TRAVEL', 'MEALS')),
            receipt_required boolean NOT NULL DEFAULT true,
            per_km           boolean NOT NULL DEFAULT false,
            -- Nothing is assumed tax-free: every category starts taxable and HR changes it.
            taxable          boolean NOT NULL DEFAULT true,
            active           boolean NOT NULL DEFAULT true,
            sort_order       integer NOT NULL
        )
        """
    )
    op.execute(
        """
        INSERT INTO hr.claim_category (category_code, label, kind, receipt_required, per_km, sort_order)
        VALUES
            ('BIKE_TAXI', 'Bike taxi', 'TRAVEL', true, false, 1),
            ('CAR_TAXI', 'Car or taxi', 'TRAVEL', true, false, 2),
            ('PERSONAL_BIKE', 'Personal bike (per km)', 'TRAVEL', false, true, 3),
            ('OUTSTATION_TRAIN', 'Outstation train', 'TRAVEL', true, false, 4),
            ('MEALS', 'Meals', 'MEALS', true, false, 5)
        """
    )
    op.execute(
        """
        CREATE TABLE hr.claim (
            claim_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            employee_id     uuid NOT NULL REFERENCES hr.employee (employee_id),
            category_code   text NOT NULL REFERENCES hr.claim_category (category_code),
            expense_date    date NOT NULL,
            amount          numeric(12, 2) NOT NULL CHECK (amount > 0),
            distance_km     numeric(8, 1),
            description     text,
            status          text NOT NULL DEFAULT 'SUBMITTED' CHECK (status IN
                ('SUBMITTED', 'CORRECTION_REQUESTED', 'APPROVED', 'HANDED_TO_PAYROLL', 'PAID',
                 'REJECTED', 'CANCELLED')),
            -- The reviews this claim needs, in order, and how far it has got.
            stage_plan      text[] NOT NULL DEFAULT '{}',
            stage_index     integer NOT NULL DEFAULT 0,
            stale           boolean NOT NULL DEFAULT false,
            -- The payroll month it goes into: submitted by the cut-off day, that month; else the next.
            payroll_month   date NOT NULL,
            payroll_run_id  uuid,
            submitted_at    timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            decided_at      timestamptz,
            CHECK (payroll_month = date_trunc('month', payroll_month)::date)
        )
        """
    )
    op.execute("CREATE INDEX claim_employee_ix ON hr.claim (employee_id, expense_date)")
    op.execute("CREATE INDEX claim_status_ix ON hr.claim (status, payroll_month)")
    op.execute(
        """
        CREATE TABLE hr.claim_receipt (
            receipt_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            claim_id      uuid NOT NULL REFERENCES hr.claim (claim_id),
            file_key      text NOT NULL,
            content_type  text NOT NULL,
            original_name text,
            size_bytes    integer NOT NULL,
            uploaded_at   timestamptz NOT NULL DEFAULT now(),
            removed_at    timestamptz
        )
        """
    )
    op.execute("CREATE INDEX claim_receipt_claim_ix ON hr.claim_receipt (claim_id)")
    op.execute(
        """
        CREATE TABLE hr.claim_event (
            event_id    bigserial PRIMARY KEY,
            claim_id    uuid NOT NULL REFERENCES hr.claim (claim_id),
            occurred_at timestamptz NOT NULL DEFAULT now(),
            actor       text,
            event_type  text NOT NULL,
            stage       text,
            note        text
        )
        """
    )
    op.execute("CREATE INDEX claim_event_claim_ix ON hr.claim_event (claim_id, event_id)")


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
