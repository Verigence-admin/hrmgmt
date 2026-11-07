"""Birthday leave grants written at an older quota are corrected to the current one
(product owner, 7 Nov 2026).

A SPECIAL grant larger than `leave.special_days_per_year` (as set in hr.setting) gets one ADJUST
entry bringing it down to the setting, with a note starting "Quota correction". Nothing is updated
or deleted: the ledger stays append-only and the history shows both rows. Balances count the
correction against what was granted, not as leave used.

Left alone: grants where more than the setting has already been used; grants whose birthday came
before the person joined (that birthday opens nothing, so the grant is never read: the joining
rule wins); and anything already corrected (running this again adds nothing). Without a setting
row there is nothing to correct against.

Revision ID: 0027_birthday_quota_correction
Revises: 0026_working_sunday
"""

from alembic import op

revision = "0027_birthday_quota_correction"
down_revision = "0026_working_sunday"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO hr.leave_ledger (employee_id, leave_type, leave_year, entry_type, days, note)
        SELECT g.employee_id, 'SPECIAL', g.leave_year, 'ADJUST', q.target - g.days,
               'Quota correction: birthday leave is ' || q.target || ' day(s) per birthday;'
               || ' this grant was written at ' || g.days || ' (product owner 7 Nov 2026)'
        FROM hr.leave_ledger g
        JOIN hr.employee e ON e.employee_id = g.employee_id
        CROSS JOIN (
            SELECT (value #>> '{}')::numeric AS target FROM hr.setting
            WHERE key = 'leave.special_days_per_year'
        ) q
        WHERE g.leave_type = 'SPECIAL' AND g.entry_type = 'GRANT' AND g.days > q.target
          AND (
              SELECT coalesce(-sum(u.days), 0) FROM hr.leave_ledger u
              WHERE u.employee_id = g.employee_id AND u.leave_type = 'SPECIAL'
                AND u.leave_year = g.leave_year AND u.entry_type IN ('DEDUCT', 'REVERSAL')
          ) <= q.target
          AND NOT EXISTS (
              SELECT 1 FROM hr.leave_ledger c
              WHERE c.employee_id = g.employee_id AND c.leave_type = 'SPECIAL'
                AND c.leave_year = g.leave_year AND c.entry_type = 'ADJUST'
                AND c.note LIKE 'Quota correction%'
          )
          AND NOT (
              e.date_of_birth IS NOT NULL AND e.date_of_joining IS NOT NULL
              AND make_date(
                  g.leave_year,
                  extract(month FROM e.date_of_birth)::int,
                  LEAST(
                      extract(day FROM e.date_of_birth)::int,
                      extract(day FROM (make_date(g.leave_year,
                          extract(month FROM e.date_of_birth)::int, 1)
                          + interval '1 month - 1 day'))::int
                  )
              ) < e.date_of_joining
          )
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
