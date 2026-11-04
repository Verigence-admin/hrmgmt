"""The Monday 09:00 approvals deadline (product owner, 4-5 Nov 2026).

At 09:00 IST every Monday, every Team Lead with an attendance, leave or claim request raised the
previous week (or earlier) still waiting on them has their whole Verigence access paused in Security
until SuperAdmin restores it from myHR Settings. A request waits with every Team Lead of the
person's project, so on a project with two Team Leads both are paused. Project Managers are not.
Each Monday is processed once; a check that misses 09:00 (a restart) runs at the next check."""

from __future__ import annotations

import json
import threading
from datetime import datetime, time, timedelta
from typing import Any

import structlog
from sqlalchemy import Connection, Engine, text

from hrmgmt.provisioning import ProvisioningError, UserProvisioner
from hrmgmt.timeutil import IST, to_ist, utc_now

logger = structlog.get_logger(__name__)

REASON = "Approvals waiting with you passed the Monday 09:00 deadline"

# The rule was announced on 5 Nov 2026: the first deadline enforced is the Monday after. Earlier
# Mondays are never processed, so no one is paused for a week in which the rule did not exist.
FIRST_DEADLINE = datetime(2026, 11, 9, 9, 0, tzinfo=IST)


def latest_deadline(now: datetime) -> datetime:
    """The most recent Monday 09:00 IST at or before `now`."""
    local = to_ist(now)
    monday = local.date() - timedelta(days=local.weekday())
    deadline = datetime.combine(monday, time(9, 0), tzinfo=IST)
    if local < deadline:
        deadline -= timedelta(days=7)
    return deadline


def overdue_team_leads(conn: Connection, deadline: datetime) -> dict[str, list[dict[str, Any]]]:
    """Team Lead user id -> the requests waiting on them that were due by `deadline`."""
    from hrmgmt.api.oversight import _build

    rows, _, by_approver = _build(conn, None, deadline)
    out: dict[str, list[dict[str, Any]]] = {}
    for uid, row in rows.items():
        if not any(r["role"] == "TL" for r in row["roles"]):
            continue
        late = [
            item
            for item in by_approver.get(uid, [])
            if datetime.fromisoformat(item["deadline"]) <= deadline
        ]
        if late:
            out[uid] = late
    return out


def run_deadline(
    engine: Engine, provisioner: UserProvisioner, now: datetime | None = None
) -> int | None:
    """Pauses the Team Leads past the latest deadline, once per deadline. Returns how many were
    paused, or None when this deadline was already processed."""
    now = now or utc_now()
    deadline = latest_deadline(now)
    if deadline < FIRST_DEADLINE:
        return None
    with engine.begin() as conn:
        done = conn.execute(
            text("SELECT 1 FROM hr.approver_deadline_run WHERE deadline_at = :d FOR UPDATE"),
            {"d": deadline},
        ).first()
        if done:
            return None
        overdue = overdue_team_leads(conn, deadline)
        for uid, items in overdue.items():
            conn.execute(
                text(
                    "INSERT INTO hr.approver_pause (security_user_id, deadline_at, overdue_count,"
                    " overdue_items) VALUES (CAST(:u AS uuid), :d, :n, CAST(:i AS jsonb))"
                    " ON CONFLICT (security_user_id, deadline_at) DO NOTHING"
                ),
                {"u": uid, "d": deadline, "n": len(items), "i": json.dumps(items)},
            )
        conn.execute(
            text("INSERT INTO hr.approver_deadline_run (deadline_at, paused) VALUES (:d, :n)"),
            {"d": deadline, "n": len(overdue)},
        )
    # Security is told after the record is safe; a refusal is kept on the row for SuperAdmin.
    for uid in overdue:
        error: str | None = None
        try:
            provisioner.pause_access(user_id=uid, reason=REASON)
        except ProvisioningError as exc:
            error = f"{exc.code}: {exc}"
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE hr.approver_pause SET security_paused = :ok, security_error = :e"
                    " WHERE security_user_id = CAST(:u AS uuid) AND deadline_at = :d"
                ),
                {"ok": error is None, "e": error, "u": uid, "d": deadline},
            )
    logger.info("hr_approver_deadline_run", paused=len(overdue))
    return len(overdue)


class WeeklyDeadline:
    """Checks every ten minutes whether a Monday 09:00 deadline is unprocessed."""

    def __init__(self, engine: Engine, provisioner: UserProvisioner, *, every_s: float = 600.0):
        self._engine = engine
        self._provisioner = provisioner
        self._every = every_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="hr-approver-deadline", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self._stop.wait(60):
            return
        while not self._stop.is_set():
            try:
                run_deadline(self._engine, self._provisioner)
            except Exception as exc:
                logger.warning("hr_approver_deadline_error", error_type=type(exc).__name__)
            self._stop.wait(self._every)
