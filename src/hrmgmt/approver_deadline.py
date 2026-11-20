"""The Monday 09:00 approvals deadline (product owner, 4-5 Nov 2026).

At 09:00 IST every Monday, every Team Lead with an attendance, leave or claim request raised the
previous week (or earlier) still waiting on them has their whole Verigence access paused in Security
until SuperAdmin restores it from myHR Settings. A request waits with every Team Lead of the
person's project, so on a project with two Team Leads both are paused. Project Managers are not.
Each Monday is processed once. The job wakes at 09:00 by the clock and once when the service
starts (a restart that missed 09:00); between the two it asks the database nothing."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import datetime, time, timedelta
from typing import Any

import structlog
from sqlalchemy import Connection, Engine, text

from hrmgmt import sleep_window
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


def next_deadline(now: datetime) -> datetime:
    """The first Monday 09:00 IST after `now`."""
    return latest_deadline(now) + timedelta(days=7)


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
    """Runs the deadline at Monday 09:00 IST by the clock, and once when the service starts.

    It used to ask the database every ten minutes whether a deadline was unprocessed, day and
    night, which alone kept the database from ever stopping at night (20 Nov 2026). The deadline
    is a fixed time, so the job now waits for it without the database. Only a run that failed
    (database or Security unreachable) is tried again ten minutes later, until it goes through."""

    def __init__(
        self,
        engine: Engine,
        provisioner: UserProvisioner,
        *,
        retry_s: float = 600.0,
        start_delay_s: float = 60.0,
        clock: Callable[[], datetime] = utc_now,
    ):
        self._engine = engine
        self._provisioner = provisioner
        self._retry = retry_s
        self._start_delay = start_delay_s
        self._clock = clock
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="hr-approver-deadline", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _attempt(self) -> bool:
        try:
            run_deadline(self._engine, self._provisioner, self._clock())
        except Exception as exc:
            logger.warning("hr_approver_deadline_error", error_type=type(exc).__name__)
            return False
        return True

    def seconds_to_next(self) -> float:
        """From now to a second past the next Monday 09:00 IST."""
        now = self._clock()
        return (next_deadline(now) - now).total_seconds() + 1.0

    def _loop(self) -> None:
        # Let the service finish starting; then the deadline a restart may have missed. Started
        # inside the night's sleep, that waits for the morning: 09:00 is after the window.
        if self._stop.wait(self._start_delay + sleep_window.seconds_until_wake(self._clock())):
            return
        done = self._attempt()
        while not self._stop.is_set():
            retry = sleep_window.outside_sleep(self._retry, self._clock())
            if self._stop.wait(self.seconds_to_next() if done else retry):
                return
            done = self._attempt()
