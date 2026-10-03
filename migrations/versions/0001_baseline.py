"""HR baseline: schema, append-only audit log, designations.

Revision ID: 0001_baseline
Revises:
"""

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS hr")

    # One row per HR or payroll action. Append-only: rows can never be changed or removed,
    # not even by the service itself (enforced by triggers below).
    op.execute(
        """
        CREATE TABLE hr.audit_log (
            audit_id      bigserial PRIMARY KEY,
            occurred_at   timestamptz NOT NULL DEFAULT now(),
            actor_user_id text NOT NULL,
            action        text NOT NULL,
            entity_type   text NOT NULL,
            entity_id     text NOT NULL,
            changes       jsonb NOT NULL DEFAULT '{}'::jsonb,
            request_id    text,
            client_ip     text
        )
        """
    )
    op.execute(
        "CREATE INDEX audit_log_entity_idx ON hr.audit_log (entity_type, entity_id, audit_id DESC)"
    )
    op.execute("CREATE INDEX audit_log_actor_idx ON hr.audit_log (actor_user_id, audit_id DESC)")
    op.execute(
        """
        CREATE FUNCTION hr.audit_log_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        BEGIN
            RAISE EXCEPTION 'hr.audit_log is append-only';
        END;
        $fn$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_update_delete
            BEFORE UPDATE OR DELETE ON hr.audit_log
            FOR EACH ROW EXECUTE FUNCTION hr.audit_log_append_only()
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_truncate
            BEFORE TRUNCATE ON hr.audit_log
            FOR EACH STATEMENT EXECUTE FUNCTION hr.audit_log_append_only()
        """
    )

    # Designation: what the person is in the company. Four fixed values, assigned by HRAdmin.
    # It grants no access by itself (access comes from project Role and HR-module roles).
    op.execute(
        """
        CREATE TABLE hr.designation (
            code       text PRIMARY KEY,
            label      text NOT NULL,
            sort_order integer NOT NULL,
            active     boolean NOT NULL DEFAULT true
        )
        """
    )
    op.execute(
        """
        INSERT INTO hr.designation (code, label, sort_order) VALUES
            ('AUDITOR', 'Auditor', 1),
            ('SENIOR_AUDITOR', 'Senior Auditor', 2),
            ('ASSISTANT_MANAGER', 'Assistant Manager', 3),
            ('MANAGER', 'Manager', 4)
        """
    )


def downgrade() -> None:
    # Deliberately unsupported: this schema holds payroll and personal data, and a downgrade
    # must never be a one-command way to delete it.
    raise RuntimeError("HR migrations are forward-only")
