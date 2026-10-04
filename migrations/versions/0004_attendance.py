"""Settings, holidays, the copy of work assignments from Audit Core, and attendance.

Revision ID: 0004_attendance
Revises: 0003_profile_qual
"""

from alembic import op

revision = "0004_attendance"
down_revision = "0003_profile_qual"
branch_labels = None
depends_on = None

_TENTATIVE_HOLIDAYS_2026 = [
    ("2026-10-19", "Mahanavami"),
    ("2026-10-20", "Vijaya Dasami"),
    ("2026-11-08", "Diwali"),
    ("2026-11-24", "Rahas Purnima"),
    ("2026-12-25", "X-Mas"),
]


def upgrade() -> None:
    # Company-wide values HR may change. Defaults live in code; a row here overrides one.
    op.execute(
        """
        CREATE TABLE hr.setting (
            key         text PRIMARY KEY,
            value       jsonb NOT NULL,
            updated_at  timestamptz NOT NULL DEFAULT now(),
            updated_by  text
        )
        """
    )
    # Only a DECLARED holiday is a non-working day. A TENTATIVE one is shown with a caveat.
    op.execute(
        """
        CREATE TABLE hr.holiday (
            holiday_date date PRIMARY KEY,
            name         text NOT NULL,
            status       text NOT NULL DEFAULT 'TENTATIVE' CHECK (status IN ('TENTATIVE', 'DECLARED')),
            updated_at   timestamptz NOT NULL DEFAULT now(),
            updated_by   text
        )
        """
    )
    for day, name in _TENTATIVE_HOLIDAYS_2026:
        op.execute(
            f"INSERT INTO hr.holiday (holiday_date, name, status) VALUES ('{day}', '{name}', 'TENTATIVE')"
        )

    # HR's own copy of who works where, refreshed from Audit Core once a day. No HR request ever
    # waits on Audit Core. A row that disappears from Audit Core is closed (valid_to), not deleted,
    # so a past day is judged against the assignment that was valid on that day.
    op.execute(
        """
        CREATE TABLE hr.work_assignment (
            assignment_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            security_user_id uuid NOT NULL,
            tenant_id        text NOT NULL,
            project_code     text,
            project_name     text,
            role_code        text NOT NULL,
            dealer_name      text,
            outlet_id        uuid,
            outlet_code      text,
            outlet_name      text,
            latitude         numeric(9, 6),
            longitude        numeric(9, 6),
            valid_from       timestamptz NOT NULL,
            valid_to         timestamptz,
            last_seen_at     timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX work_assignment_identity_uq ON hr.work_assignment
            (security_user_id, tenant_id, role_code,
             coalesce(outlet_id, '00000000-0000-0000-0000-000000000000'::uuid), valid_from)
        """
    )
    op.execute(
        "CREATE INDEX work_assignment_user_ix ON hr.work_assignment (security_user_id, valid_to)"
    )
    op.execute(
        "CREATE INDEX work_assignment_tenant_ix ON hr.work_assignment (tenant_id, role_code)"
    )
    op.execute(
        """
        CREATE TABLE hr.work_sync (
            singleton        boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            last_attempt_at  timestamptz,
            last_success_at  timestamptz,
            last_status      text,
            last_error       text,
            assignments_seen integer
        )
        """
    )
    op.execute("INSERT INTO hr.work_sync (singleton) VALUES (true)")

    # A photo is accepted only with a one-time token the server issued moments before.
    op.execute(
        """
        CREATE TABLE hr.capture_token (
            token_hash  text PRIMARY KEY,
            employee_id uuid NOT NULL REFERENCES hr.employee (employee_id),
            purpose     text NOT NULL CHECK (purpose IN ('CHECK_IN', 'CHECK_OUT')),
            created_at  timestamptz NOT NULL DEFAULT now(),
            expires_at  timestamptz NOT NULL,
            used_at     timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX capture_token_employee_ix ON hr.capture_token (employee_id, created_at)"
    )

    op.execute(
        """
        CREATE TABLE hr.attendance_day (
            attendance_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            employee_id            uuid NOT NULL REFERENCES hr.employee (employee_id),
            work_date              date NOT NULL,
            geofenced              boolean NOT NULL DEFAULT false,
            check_in_at            timestamptz,
            check_in_lat           numeric(9, 6),
            check_in_lon           numeric(9, 6),
            check_in_accuracy_m    numeric(8, 2),
            check_in_distance_m    numeric(10, 1),
            check_in_outlet_id     uuid,
            check_in_outlet_name   text,
            check_in_address       text,
            check_in_photo_key     text,
            check_in_flags         jsonb NOT NULL DEFAULT '[]'::jsonb,
            check_out_at           timestamptz,
            check_out_lat          numeric(9, 6),
            check_out_lon          numeric(9, 6),
            check_out_accuracy_m   numeric(8, 2),
            check_out_distance_m   numeric(10, 1),
            check_out_outlet_id    uuid,
            check_out_outlet_name  text,
            check_out_address      text,
            check_out_photo_key    text,
            check_out_flags        jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at             timestamptz NOT NULL DEFAULT now(),
            updated_at             timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT attendance_day_one_per_employee_uq UNIQUE (employee_id, work_date),
            CHECK (check_out_at IS NULL OR check_in_at IS NOT NULL)
        )
        """
    )
    op.execute("CREATE INDEX attendance_day_date_ix ON hr.attendance_day (work_date)")

    # A late check-in, an early check-out, or a position outside the fence waits here for a Team
    # Lead or Project Manager (or HR when the person has none). Until approved the day is not
    # counted as a normal day.
    op.execute(
        """
        CREATE TABLE hr.attendance_exception (
            exception_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            attendance_id uuid NOT NULL REFERENCES hr.attendance_day (attendance_id),
            employee_id   uuid NOT NULL REFERENCES hr.employee (employee_id),
            work_date     date NOT NULL,
            event         text NOT NULL CHECK (event IN ('CHECK_IN', 'CHECK_OUT')),
            kind          text NOT NULL CHECK (kind IN
                ('LATE_CHECK_IN', 'EARLY_CHECK_OUT', 'OUT_OF_FENCE', 'NO_OUTLET_LOCATION')),
            reason        text,
            status        text NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED')),
            created_at    timestamptz NOT NULL DEFAULT now(),
            decided_by    text,
            decided_at    timestamptz,
            decision_note text
        )
        """
    )
    op.execute(
        "CREATE INDEX attendance_exception_status_ix ON hr.attendance_exception (status, work_date)"
    )
    op.execute(
        "CREATE INDEX attendance_exception_employee_ix ON hr.attendance_exception (employee_id, work_date)"
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
