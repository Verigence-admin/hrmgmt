"""An employee asks HR to change their email or mobile, with the old and the new value.

HR approves or rejects. The change (and the Verigence login, when there is one) happens only on approval.

Revision ID: 0020_contact_change_request
Revises: 0019_face_match
"""

from alembic import op

revision = "0020_contact_change_request"
down_revision = "0019_face_match"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE hr.employee_contact_change (
            change_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            employee_id   uuid NOT NULL REFERENCES hr.employee (employee_id),
            field         text NOT NULL CHECK (field IN ('EMAIL', 'MOBILE')),
            old_value     text,
            new_value     text NOT NULL,
            status        text NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')),
            requested_by  text NOT NULL,
            requested_at  timestamptz NOT NULL DEFAULT now(),
            decided_by    text,
            decided_at    timestamptz,
            decision_note text,
            -- What happened to the Verigence login when it was approved.
            login_outcome text
        )
        """
    )
    # One open request per person and field: a second one waits for the first to be decided.
    op.execute(
        "CREATE UNIQUE INDEX employee_contact_change_open_uq"
        " ON hr.employee_contact_change (employee_id, field) WHERE status = 'PENDING'"
    )
    op.execute(
        "CREATE INDEX employee_contact_change_ix"
        " ON hr.employee_contact_change (status, requested_at DESC)"
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
