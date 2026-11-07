"""Half-day holidays, and holidays for chosen projects and outlets (product owner, 7 Nov 2026).

A holiday becomes a row with its own id, so several can share a date (different scopes or halves):
- day_part: FULL_DAY, or MORNING / AFTERNOON off;
- applies_to: ALL (the whole company) or SELECTED, with scope rows in hr.holiday_scope (a whole
  project: tenant with no outlet; or one outlet of a project);
- removed_at / removed_by: removing a holiday keeps the row, so history stays readable.
Every existing row becomes a FULL_DAY holiday for ALL, with its date, name, status and who last
changed it kept as they were.

Work on the off half of a half-day holiday earns half a comp-off day: the off-day work exception
now records what its approval earns (comp_off_days; empty on older rows means 1).

Revision ID: 0025_holiday_scope
Revises: 0024_attendance_correction
"""

from alembic import op

revision = "0025_holiday_scope"
down_revision = "0024_attendance_correction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE hr.holiday ADD COLUMN holiday_id uuid NOT NULL DEFAULT gen_random_uuid()"
    )
    op.execute(
        "ALTER TABLE hr.holiday ADD COLUMN day_part text NOT NULL DEFAULT 'FULL_DAY'"
        " CHECK (day_part IN ('FULL_DAY', 'MORNING', 'AFTERNOON'))"
    )
    op.execute(
        "ALTER TABLE hr.holiday ADD COLUMN applies_to text NOT NULL DEFAULT 'ALL'"
        " CHECK (applies_to IN ('ALL', 'SELECTED'))"
    )
    op.execute("ALTER TABLE hr.holiday ADD COLUMN created_at timestamptz")
    op.execute("ALTER TABLE hr.holiday ADD COLUMN created_by text")
    op.execute("UPDATE hr.holiday SET created_at = updated_at, created_by = updated_by")
    op.execute("ALTER TABLE hr.holiday ALTER COLUMN created_at SET DEFAULT now()")
    op.execute("ALTER TABLE hr.holiday ALTER COLUMN created_at SET NOT NULL")
    op.execute("ALTER TABLE hr.holiday ADD COLUMN removed_at timestamptz")
    op.execute("ALTER TABLE hr.holiday ADD COLUMN removed_by text")
    op.execute("ALTER TABLE hr.holiday DROP CONSTRAINT holiday_pkey")
    op.execute("ALTER TABLE hr.holiday ADD CONSTRAINT holiday_pkey PRIMARY KEY (holiday_id)")
    op.execute(
        "CREATE INDEX holiday_live_date_ix ON hr.holiday (holiday_date) WHERE removed_at IS NULL"
    )
    # One live company-wide holiday per date and day part; overlaps across parts and scopes are
    # refused by the service with a message that names the holiday in the way.
    op.execute(
        "CREATE UNIQUE INDEX holiday_company_part_ux ON hr.holiday (holiday_date, day_part)"
        " WHERE applies_to = 'ALL' AND removed_at IS NULL"
    )

    op.execute(
        """
        CREATE TABLE hr.holiday_scope (
            scope_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            holiday_id   uuid NOT NULL REFERENCES hr.holiday (holiday_id) ON DELETE CASCADE,
            tenant_id    text NOT NULL,
            project_code text,
            project_name text,
            outlet_id    uuid,
            outlet_code  text,
            outlet_name  text
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX holiday_scope_uq ON hr.holiday_scope (holiday_id, tenant_id,"
        " coalesce(outlet_id, '00000000-0000-0000-0000-000000000000'::uuid))"
    )

    op.execute(
        "ALTER TABLE hr.attendance_exception ADD COLUMN comp_off_days numeric(2, 1)"
        " CHECK (comp_off_days IS NULL OR comp_off_days IN (0.5, 1))"
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
