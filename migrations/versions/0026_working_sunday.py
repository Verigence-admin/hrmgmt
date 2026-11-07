"""A Sunday made a working day for chosen projects or for everyone (product owner, 7 Nov 2026).

Every Sunday is a holiday by default. HR can make a given Sunday a working day; it is a row of
hr.holiday with kind WORKING_DAY (a full day, a Sunday, declared), for the whole company or with
scope rows like any holiday. Every existing row is a HOLIDAY.

Revision ID: 0026_working_sunday
Revises: 0025_holiday_scope
"""

from alembic import op

revision = "0026_working_sunday"
down_revision = "0025_holiday_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE hr.holiday ADD COLUMN kind text NOT NULL DEFAULT 'HOLIDAY'"
        " CHECK (kind IN ('HOLIDAY', 'WORKING_DAY'))"
    )
    # A working day is a whole Sunday, and it is either set or not: no half day, no tentative.
    op.execute(
        "ALTER TABLE hr.holiday ADD CONSTRAINT holiday_working_day_shape CHECK (kind = 'HOLIDAY'"
        " OR (day_part = 'FULL_DAY' AND status = 'DECLARED' AND extract(isodow FROM holiday_date) = 7))"
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
