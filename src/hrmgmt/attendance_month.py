"""A person's month of attendance in numbers, and the monthly limits (product owner, 16 Nov 2026).

For a calendar month, up to today:

- Late check-ins: days checked in after the "late check-in after" time (the days that raise a late
  exception). How late is measured from the standard check-in time, the time the person should
  have arrived.
- Early check-outs: days checked out before the "earliest check-out" time. How early is measured
  to the standard check-out time.
- Average hours a day: check-out minus check-in, over the full working days with both (Sundays,
  holidays, half-day holidays and half-day leave are left out, as they are short by design).

Each person is expected to work `attendance.expected_daily_hours` (9) a day on average; the
average is shown, not enforced (product owner, 16 Nov 2026: "remove average rule"). When, in a
month, the late and early time adds up to more than `attendance.escalate_late_early_hours` (10), the
person's attendance requests of that month (late, early, location and short-day exceptions, and
attendance corrections) can be decided only by a Project Manager or Executive of their project
(the CEO always; HR when the project has neither). Every such request carries a note saying why,
and the person is told by email the first time it happens in a month.

A Team Lead's own attendance requests always go to a Project Manager or Executive (product owner,
16 Nov 2026), never to another Team Lead: see `approval_route`.

Times are those on the day as it stands, so an approved time correction counts. A day off
(Sunday, declared holiday) is never late or early."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import Connection, text

from hrmgmt import holidays as hol
from hrmgmt import settings_store as cfg
from hrmgmt.attendance_grace import month_bounds
from hrmgmt.timeutil import ist_date, parse_hhmm, to_ist

logger = structlog.get_logger(__name__)

LATE_EARLY_HOURS = "LATE_EARLY_HOURS"
NOTICE_TEMPLATE = "ATTENDANCE_ESCALATION"
ESCALATED_ROLES = ("PM", "EXECUTIVE")


def hm(minutes: int) -> str:
    """ "3 h 05 m"."""
    minutes = max(0, int(minutes))
    return f"{minutes // 60} h {minutes % 60:02d} m"


@dataclass
class MonthStats:
    month: date
    to: date  # the last day counted
    late_count: int = 0
    late_minutes: int = 0
    early_count: int = 0
    early_minutes: int = 0
    days_clocked: int = 0
    minutes_clocked: int = 0
    expected_hours: int = 9
    limit_late_early_hours: int = 10
    reasons: list[str] = field(default_factory=list)

    @property
    def late_early_minutes(self) -> int:
        return self.late_minutes + self.early_minutes

    @property
    def average_minutes(self) -> int | None:
        if self.days_clocked == 0:
            return None
        return round(self.minutes_clocked / self.days_clocked)

    @property
    def escalated(self) -> bool:
        return bool(self.reasons)

    def note(self) -> str | None:
        """Why the requests of this month go to a Project Manager or Executive, in plain words."""
        if not self.reasons:
            return None
        return (
            "Only a Project Manager or Executive can approve this: late check-ins and early"
            f" check-outs add up to {hm(self.late_early_minutes)} in {self.month:%B %Y} (more than"
            f" {self.limit_late_early_hours} hours)."
        )

    def view(self) -> dict[str, Any]:
        average = self.average_minutes
        return {
            "month": self.month.strftime("%Y-%m"),
            "countedTo": self.to.isoformat(),
            "lateCheckIns": self.late_count,
            "lateMinutes": self.late_minutes,
            "earlyCheckOuts": self.early_count,
            "earlyMinutes": self.early_minutes,
            "lateEarlyMinutes": self.late_early_minutes,
            "daysClocked": self.days_clocked,
            "averageMinutes": average,
            "expectedHours": self.expected_hours,
            "limits": {"lateEarlyHours": self.limit_late_early_hours},
            "escalated": self.escalated,
            "reasons": list(self.reasons),
            "note": self.note(),
        }


def _minutes_of(at: datetime) -> int:
    local = to_ist(at)
    return local.hour * 60 + local.minute


def _hhmm_minutes(value: str) -> int:
    t = parse_hhmm(value)
    return t.hour * 60 + t.minute


def month_stats(
    conn: Connection,
    employee_id: str,
    day: date,
    now: datetime,
    values: dict[str, Any] | None = None,
) -> MonthStats:
    """The month holding `day`, counted up to today (or the month's last day if it is over)."""
    values = values if values is not None else cfg.load_all(conn)
    first, nxt = month_bounds(day)
    today = ist_date(now)
    last = min(nxt - timedelta(days=1), today)
    stats = MonthStats(
        month=first,
        to=last,
        expected_hours=int(values["attendance.expected_daily_hours"]),
        limit_late_early_hours=int(values["attendance.escalate_late_early_hours"]),
    )
    if last < first:
        return stats
    rows = conn.execute(
        text(
            "SELECT work_date, check_in_at, check_out_at FROM hr.attendance_day"
            " WHERE employee_id = CAST(:e AS uuid) AND work_date >= :a AND work_date <= :b"
            " AND check_in_at IS NOT NULL ORDER BY work_date"
        ),
        {"e": employee_id, "a": first, "b": last},
    ).all()
    if not rows:
        return stats
    holidays = hol.holidays_for(conn, employee_id, first, last)
    half_leave = {
        r[0]
        for r in conn.execute(
            text(
                "SELECT from_date FROM hr.leave_request WHERE employee_id = CAST(:e AS uuid)"
                " AND status = 'APPROVED' AND half_day AND from_date >= :a AND from_date <= :b"
            ),
            {"e": employee_id, "a": first, "b": last},
        )
    }
    for work_date, check_in_at, check_out_at in rows:
        holiday = holidays.get(work_date)
        if hol.is_off_day(work_date, holiday):
            continue
        half = hol.half_day(work_date, holiday)
        v = hol.effective_values(values, half.day_part if half else None)
        came = _minutes_of(check_in_at)
        if came > _hhmm_minutes(v["attendance.late_after"]):
            stats.late_count += 1
            stats.late_minutes += came - _hhmm_minutes(v["attendance.check_in_standard"])
        if check_out_at is None:
            continue
        left = _minutes_of(check_out_at)
        if left < _hhmm_minutes(v["attendance.check_out_earliest"]):
            stats.early_count += 1
            stats.early_minutes += max(0, _hhmm_minutes(v["attendance.check_out_standard"]) - left)
        if half is None and work_date not in half_leave:
            stats.days_clocked += 1
            stats.minutes_clocked += max(0, int((check_out_at - check_in_at).total_seconds() // 60))
    if stats.late_early_minutes > stats.limit_late_early_hours * 60:
        stats.reasons.append(LATE_EARLY_HOURS)
    return stats


TEAM_LEAD_NOTE = (
    "Only a Project Manager or Executive can approve a Team Lead's attendance requests."
)


@dataclass(frozen=True)
class ApprovalRoute:
    """Who decides a person's attendance request of a given month: `pm_only` means a Project
    Manager or Executive (not a Team Lead), with the note saying why."""

    pm_only: bool
    note: str | None


def approval_route(
    conn: Connection, employee_id: str, stats: MonthStats, at: datetime
) -> ApprovalRoute:
    """A Team Lead's own requests (product owner, 16 Nov 2026) and the requests of a month past the
    late/early limit go to a Project Manager or Executive."""
    from hrmgmt import workcontext as wc

    if stats.escalated:
        return ApprovalRoute(True, stats.note())
    if "TL" in wc.project_roles(conn, employee_id, at):
        return ApprovalRoute(True, TEAM_LEAD_NOTE)
    return ApprovalRoute(False, None)


class StatsCache:
    """Month figures and approval routes per (person, month) within one request, so a list of
    approvals works each person's month out once."""

    def __init__(self, conn: Connection, now: datetime) -> None:
        self.conn = conn
        self.now = now
        self.values = cfg.load_all(conn)
        self._seen: dict[tuple[str, date], MonthStats] = {}
        self._routes: dict[tuple[str, date], ApprovalRoute] = {}

    def get(self, employee_id: str, day: date) -> MonthStats:
        key = (employee_id, day.replace(day=1))
        if key not in self._seen:
            self._seen[key] = month_stats(self.conn, employee_id, day, self.now, self.values)
        return self._seen[key]

    def route(self, employee_id: str, day: date) -> ApprovalRoute:
        key = (employee_id, day.replace(day=1))
        if key not in self._routes:
            self._routes[key] = approval_route(
                self.conn, employee_id, self.get(employee_id, day), self.now
            )
        return self._routes[key]


# ---- telling the person ---------------------------------------------------------------------


@dataclass(frozen=True)
class EscalationNotice:
    user_id: str
    employee_name: str
    note: str
    month: date
    company: str
    sign_in_link: str

    def subject(self) -> str:
        return f"Your attendance for {self.month:%B %Y}: approvals now go to your Project Manager"

    def body(self) -> str:
        lines = [
            f"Dear {self.employee_name},",
            "",
            self.note.replace(
                "Only a Project Manager or Executive can approve this: ", "In ", 1
            ).rstrip(".")
            + ".",
            "",
            f"From now on, your late check-ins, early check-outs, location and short-day requests"
            f" and your attendance corrections for {self.month:%B %Y} will be approved only by your"
            " Project Manager or Executive, not your Team Lead.",
            "",
            "You can see your late and early time and your average hours in myHR, under Attendance.",
        ]
        if self.sign_in_link:
            lines += ["", f"Open Verigence: {self.sign_in_link}"]
        lines += ["", f"HR, {self.company}"]
        return "\n".join(lines)


def record_escalation(
    conn: Connection, employee_id: str, day: date, now: datetime
) -> EscalationNotice | None:
    """When the person's month has passed a limit and they have not been told yet this month,
    records it and returns the email to send (after the answer goes back). Otherwise None."""
    stats = month_stats(conn, employee_id, day, now)
    if not stats.escalated:
        return None
    inserted = conn.execute(
        text(
            "INSERT INTO hr.attendance_escalation (employee_id, month, reasons, created_at)"
            " VALUES (CAST(:e AS uuid), :m, CAST(:r AS jsonb), :n)"
            " ON CONFLICT (employee_id, month) DO NOTHING RETURNING 1"
        ),
        {
            "e": employee_id,
            "m": stats.month,
            "r": "[" + ",".join(f'"{r}"' for r in stats.reasons) + "]",
            "n": now,
        },
    ).first()
    if inserted is None:
        return None
    row = conn.execute(
        text(
            "SELECT full_name, security_user_id FROM hr.employee"
            " WHERE employee_id = CAST(:e AS uuid)"
        ),
        {"e": employee_id},
    ).first()
    note = stats.note()
    if row is None or row[1] is None or note is None:
        return None
    values = cfg.load_all(conn)
    return EscalationNotice(
        user_id=str(row[1]),
        employee_name=str(row[0]),
        note=note,
        month=stats.month,
        company=str(values.get("company.name") or "").strip() or "Verigence",
        sign_in_link=str(values.get("email.sign_in_url") or ""),
    )


def notify_employee(notice: EscalationNotice, *, provisioner: Any, mailer: Any) -> str:
    """Emails the person once. Runs after the answer has gone back; one attempt, no retries; the
    outcome is written to the message log. Returns SENT, SKIPPED or FAILED."""
    status, reason, name = "SKIPPED", None, None
    if mailer is None or provisioner is None:
        reason = "MAIL_NOT_CONFIGURED" if mailer is None else "LOGIN_SERVICE_NOT_CONFIGURED"
    else:
        try:
            found = provisioner.list_users(ids=[notice.user_id], limit=1)
            person = next((u for u in found if u.user_id == notice.user_id), None)
            name = person.display_name if person else None
            if person is None or not person.email:
                reason = "NO_EMAIL" if person else "NOT_FOUND"
            elif person.status not in ("ACTIVE", "PENDING"):
                reason = "NOT_ACTIVE"
            else:
                mailer.send(to=person.email, subject=notice.subject(), body=notice.body())
                status = "SENT"
        except Exception as exc:  # the mail server or Security failing never matters to the caller
            status, reason = "FAILED", getattr(exc, "code", None) or type(exc).__name__
    _log(notice, status, reason, name)
    return status


def _log(notice: EscalationNotice, status: str, reason: str | None, name: str | None) -> None:
    from hrmgmt.db import get_engine

    try:
        with get_engine().begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO hr.message_log (employee_id, user_id, recipient_name, channel,"
                    " template_code, status, reason_code, sent_by)"
                    " SELECT employee_id, security_user_id, :n, 'EMAIL', :t, :s, :r, 'SYSTEM'"
                    " FROM hr.employee WHERE security_user_id = CAST(:u AS uuid)"
                ),
                {
                    "u": notice.user_id,
                    "n": name or notice.employee_name,
                    "t": NOTICE_TEMPLATE,
                    "s": status,
                    "r": reason,
                },
            )
    except Exception as exc:
        logger.warning("hr_attendance_escalation_log_failed", error_type=type(exc).__name__)
    logger.info("hr_attendance_escalation_notice", status=status, reason=reason)
