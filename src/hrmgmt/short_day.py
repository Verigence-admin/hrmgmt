"""A short day (product owner, 6 Nov 2026).

An employee who spends less than `attendance.min_work_hours` (5 to start) between check-in and
check-out on a working day raises a SHORT_DAY exception at check-out. Like a late check-in or an
early check-out, it needs a reason, it goes to the Team Lead to approve or reject (it waits in the
same approvals list and counts towards the Monday 09:00 deadline), and a rejection makes the day a
half day. The Project Managers of the person's project are told by email; they are informed, the
Team Lead decides.

Not a short day: a Sunday or declared holiday (that is off-day work, approved on its own), and a
day with approved half-day leave. On a half-day holiday (7 Nov 2026) the minimum is halved and only
the time inside the working half counts. A person who never checks out raises nothing here: HR sees that
day as "Checked in, never checked out"."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import Connection, text

from hrmgmt.mailer import Mailer, MailError
from hrmgmt.provisioning import ProvisioningError, UserProvisioner
from hrmgmt.timeutil import to_ist

logger = structlog.get_logger(__name__)

KIND = "SHORT_DAY"
NOTICE_TEMPLATE = "SHORT_DAY_PM"


def minimum(values: dict[str, Any]) -> timedelta:
    """`values` are the day's settings: on a half-day holiday min_work_hours is already halved."""
    return timedelta(hours=float(values["attendance.min_work_hours"]))


def hours_text(values: dict[str, Any]) -> str:
    return f"{float(values['attendance.min_work_hours']):g}"


def on_half_day_leave(conn: Connection, employee_id: str, day: date) -> bool:
    return (
        conn.execute(
            text(
                "SELECT 1 FROM hr.leave_request WHERE employee_id = CAST(:e AS uuid)"
                " AND status = 'APPROVED' AND half_day AND from_date <= :d AND to_date >= :d"
            ),
            {"e": employee_id, "d": day},
        ).first()
        is not None
    )


def is_short(
    conn: Connection, employee_id: str, day: date, worked: timedelta, values: dict[str, Any]
) -> bool:
    return worked < minimum(values) and not on_half_day_leave(conn, employee_id, day)


def duration_text(worked: timedelta) -> str:
    minutes = max(0, int(worked.total_seconds() // 60))
    return f"{minutes // 60} h {minutes % 60:02d} m"


@dataclass(frozen=True)
class ShortDayNotice:
    """Everything the email needs, gathered while the request still has its database connection,
    so sending it later needs nothing but Security (for the addresses) and the mail server."""

    employee_name: str
    employee_code: str
    work_date: date
    check_in_at: datetime
    check_out_at: datetime
    minimum_hours: float
    reason: str | None
    pm_user_ids: tuple[str, ...]
    company: str
    sign_in_link: str
    # On a half-day holiday: the time inside the working half, and what the day was.
    worked_in_half: timedelta | None = None
    half_day: str | None = None

    @property
    def worked(self) -> timedelta:
        if self.worked_in_half is not None:
            return self.worked_in_half
        return self.check_out_at - self.check_in_at

    def subject(self) -> str:
        return (
            f"Short day: {self.employee_name} ({self.employee_code}) on "
            f"{self.work_date:%d %b %Y}, {duration_text(self.worked)}"
        )

    def body(self, pm_name: str | None) -> str:
        times = (
            f"(check-in {to_ist(self.check_in_at):%H:%M}, check-out "
            f"{to_ist(self.check_out_at):%H:%M} IST)"
        )
        if self.half_day:
            summary = (
                f"{self.employee_name} ({self.employee_code}) worked {duration_text(self.worked)}"
                f" of the working half on {self.work_date:%d %b %Y}, a half-day holiday"
                f" ({self.half_day}) {times}, less than the {self.minimum_hours:g} hours required."
            )
        else:
            summary = (
                f"{self.employee_name} ({self.employee_code}) was checked in for "
                f"{duration_text(self.worked)} on {self.work_date:%d %b %Y} {times}, less than"
                f" the {self.minimum_hours:g} hours required."
            )
        lines = [
            f"Dear {pm_name or 'there'},",
            "",
            summary,
            "",
            f"Reason given: {self.reason or '(none)'}",
            "",
            "It has gone to the Team Lead for approval in myHR. If the Team Lead rejects it, "
            "the day counts as a half day.",
            "",
            "This is for your information.",
        ]
        if self.sign_in_link:
            lines += ["", f"Open Verigence: {self.sign_in_link}"]
        lines += ["", f"HR, {self.company}"]
        return "\n".join(lines)


def build_notice(
    conn: Connection,
    *,
    employee_id: str,
    work_date: date,
    check_in_at: datetime,
    check_out_at: datetime,
    reason: str | None,
    pm_user_ids: set[str],
    values: dict[str, Any],
    worked_in_half: timedelta | None = None,
    half_day: str | None = None,
) -> ShortDayNotice | None:
    if not pm_user_ids:
        return None
    row = conn.execute(
        text(
            "SELECT full_name, employee_code FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"
        ),
        {"e": employee_id},
    ).first()
    if row is None:
        return None
    return ShortDayNotice(
        employee_name=str(row[0]),
        employee_code=str(row[1]),
        work_date=work_date,
        check_in_at=check_in_at,
        check_out_at=check_out_at,
        minimum_hours=float(values["attendance.min_work_hours"]),
        reason=(reason or "").strip() or None,
        pm_user_ids=tuple(sorted(pm_user_ids)),
        company=str(values.get("company.name") or "").strip() or "Verigence",
        sign_in_link=str(values.get("email.sign_in_url") or ""),
        worked_in_half=worked_in_half,
        half_day=half_day,
    )


def notify_project_managers(
    notice: ShortDayNotice,
    *,
    provisioner: UserProvisioner | None,
    mailer: Mailer | None,
) -> list[tuple[str, str | None, str, str | None]]:
    """Emails each Project Manager once. Runs after the check-out has been answered, so a slow or
    failing mail server never holds up the person checking out. One attempt each, no retries; the
    outcome of each is written to the message log. Returns (user id, name, status, reason code)."""
    outcomes: list[tuple[str, str | None, str, str | None]] = []
    if mailer is None or provisioner is None:
        missing = "MAIL_NOT_CONFIGURED" if mailer is None else "LOGIN_SERVICE_NOT_CONFIGURED"
        outcomes = [(uid, None, "SKIPPED", missing) for uid in notice.pm_user_ids]
        _record(outcomes)
        return outcomes
    try:
        found = {
            u.user_id: u
            for u in provisioner.list_users(
                ids=list(notice.pm_user_ids), limit=len(notice.pm_user_ids)
            )
        }
    except ProvisioningError as exc:
        outcomes = [(uid, None, "FAILED", exc.code) for uid in notice.pm_user_ids]
        _record(outcomes)
        return outcomes
    subject = notice.subject()
    for uid in notice.pm_user_ids:
        person = found.get(uid)
        name = person.display_name if person else None
        if person is None or not person.email:
            outcomes.append((uid, name, "SKIPPED", "NO_EMAIL" if person else "NOT_FOUND"))
            continue
        if person.status not in ("ACTIVE", "PENDING"):
            outcomes.append((uid, name, "SKIPPED", "NOT_ACTIVE"))
            continue
        try:
            mailer.send(to=person.email, subject=subject, body=notice.body(name))
        except MailError as exc:
            outcomes.append((uid, name, "FAILED", exc.code))
            continue
        except Exception as exc:  # never let one address stop the others
            outcomes.append((uid, name, "FAILED", type(exc).__name__))
            continue
        outcomes.append((uid, name, "SENT", None))
    _record(outcomes)
    return outcomes


def _record(outcomes: list[tuple[str, str | None, str, str | None]]) -> None:
    from hrmgmt.db import get_engine

    try:
        with get_engine().begin() as conn:
            for uid, name, status, reason in outcomes:
                employee_id = conn.execute(
                    text(
                        "SELECT employee_id FROM hr.employee"
                        " WHERE security_user_id = CAST(:u AS uuid)"
                    ),
                    {"u": uid},
                ).scalar_one_or_none()
                conn.execute(
                    text(
                        "INSERT INTO hr.message_log (employee_id, user_id, recipient_name, channel,"
                        " template_code, status, reason_code, sent_by) VALUES (CAST(:e AS uuid),"
                        " CAST(:u AS uuid), :n, 'EMAIL', :t, :s, :r, 'SYSTEM')"
                    ),
                    {
                        "e": employee_id,
                        "u": uid,
                        "n": name,
                        "t": NOTICE_TEMPLATE,
                        "s": status,
                        "r": reason,
                    },
                )
    except Exception as exc:
        logger.warning("hr_short_day_notice_log_failed", error_type=type(exc).__name__)
    logger.info(
        "hr_short_day_notice",
        sent=sum(1 for o in outcomes if o[2] == "SENT"),
        not_sent=sum(1 for o in outcomes if o[2] != "SENT"),
    )
