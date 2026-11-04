"""Team Leads past the Monday 09:00 approvals deadline (product owner, 4-5 Nov 2026).

Whatever is raised in a week (Monday to Sunday, IST) is to be decided by 09:00 IST the following
Monday. A Team Lead with anything still waiting then has their whole Verigence access paused (in
Security) until SuperAdmin restores it from myHR Settings. One row per Team Lead and week; each
week is checked once (hr.approver_deadline_run).

Revision ID: 0022_approver_pause
Revises: 0021_earned_paid_leave
"""

from alembic import op

revision = "0022_approver_pause"
down_revision = "0021_earned_paid_leave"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE hr.approver_pause (
            pause_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            security_user_id  uuid NOT NULL,
            deadline_at       timestamptz NOT NULL,
            overdue_count     integer NOT NULL CHECK (overdue_count > 0),
            overdue_items     jsonb NOT NULL DEFAULT '[]'::jsonb,
            paused_at         timestamptz NOT NULL DEFAULT now(),
            security_paused   boolean NOT NULL DEFAULT false,
            security_error    text,
            restored_at       timestamptz,
            restored_by       text,
            restore_note      text,
            UNIQUE (security_user_id, deadline_at)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE hr.approver_deadline_run (
            deadline_at  timestamptz PRIMARY KEY,
            ran_at       timestamptz NOT NULL DEFAULT now(),
            paused       integer NOT NULL
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Migrations are forward-only.")
