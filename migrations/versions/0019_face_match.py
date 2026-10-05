"""Face match: the numbers for a face (not the photo), so attendance photos can be compared.

- hr.employee_face: the face numbers of the employee's profile photo, with the photo version they came from.
- hr.attendance_day: the face numbers of the check-in photo (to compare the check-out with when there is no
  profile photo) and, for each punch, the match score and what it was compared with.

Revision ID: 0019_face_match
Revises: 0018_employee_status
"""

from alembic import op

revision = "0019_face_match"
down_revision = "0018_employee_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE hr.employee_face (
            employee_id      uuid PRIMARY KEY REFERENCES hr.employee (employee_id),
            model            text NOT NULL,
            -- Null when no clear face was found in the profile photo.
            embedding        bytea,
            photo_updated_at timestamptz NOT NULL,
            computed_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        ALTER TABLE hr.attendance_day
            ADD COLUMN check_in_face       bytea,
            ADD COLUMN check_in_face_score numeric(5, 3),
            ADD COLUMN check_in_face_ref   text CHECK (check_in_face_ref IN ('PROFILE', 'CHECK_IN')),
            ADD COLUMN check_out_face_score numeric(5, 3),
            ADD COLUMN check_out_face_ref   text CHECK (check_out_face_ref IN ('PROFILE', 'CHECK_IN'))
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
