"""Employee master and protected identity details.

Revision ID: 0002_employee
Revises: 0001_baseline
"""

from alembic import op

revision = "0002_employee"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Everything about the person except protected identity numbers. Reads of this table never
    # carry PAN, Aadhaar or bank details.
    op.execute(
        """
        CREATE TABLE hr.employee (
            employee_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            employee_code            text NOT NULL,
            full_name                text NOT NULL,
            date_of_birth            date,
            gender                   text CHECK (gender IN ('MALE', 'FEMALE', 'OTHER')),
            mobile                   text,
            personal_email           text NOT NULL,
            secondary_email          text,
            qualification            text,
            department               text,
            designation_code         text REFERENCES hr.designation (code),
            address                  text,
            emergency_contact_name   text,
            emergency_contact_number text,
            date_of_joining          date,
            employment_status        text NOT NULL DEFAULT 'ACTIVE'
                CHECK (employment_status IN ('ACTIVE', 'INACTIVE', 'EXITED')),
            -- Link to the Verigence (Security) login. Optional: a record can exist before a
            -- login does, and a failed login creation never loses the employee.
            security_user_id         uuid,
            login_status             text NOT NULL DEFAULT 'NOT_CREATED'
                CHECK (login_status IN ('NOT_CREATED', 'CREATED', 'FAILED')),
            login_error_code         text,
            created_at               timestamptz NOT NULL DEFAULT now(),
            created_by               text NOT NULL,
            updated_at               timestamptz NOT NULL DEFAULT now(),
            updated_by               text NOT NULL
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX employee_code_uq ON hr.employee (upper(employee_code))")
    op.execute("CREATE UNIQUE INDEX employee_email_uq ON hr.employee (lower(personal_email))")
    op.execute(
        "CREATE UNIQUE INDEX employee_security_user_uq ON hr.employee (security_user_id)"
        " WHERE security_user_id IS NOT NULL"
    )

    # Protected numbers, kept apart so they are only ever read on purpose. PAN and Aadhaar are
    # stored in full as decided by the company; PAN is deliberately NOT unique (duplicates and
    # gaps in the source data are flagged for HR, not rejected). Bank details arrive with payroll.
    op.execute(
        """
        CREATE TABLE hr.employee_sensitive (
            employee_id    uuid PRIMARY KEY REFERENCES hr.employee (employee_id),
            pan            text CHECK (pan ~ '^[A-Z]{5}[0-9]{4}[A-Z]$'),
            aadhaar        text CHECK (aadhaar ~ '^[0-9]{12}$'),
            updated_at     timestamptz NOT NULL DEFAULT now(),
            updated_by     text NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX employee_sensitive_pan_idx ON hr.employee_sensitive (pan) WHERE pan IS NOT NULL"
    )


def downgrade() -> None:
    raise RuntimeError("HR migrations are forward-only")
