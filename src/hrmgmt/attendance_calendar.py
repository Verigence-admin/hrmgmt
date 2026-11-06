"""What each day was for a person, with the hours they clocked (product owner, 6 Nov 2026).

One place decides a day's status, so the employee's own calendar, a lead's view of a person and the
project-wise weekly view never disagree:

- A Sunday or a declared holiday is a day off (work on it shows, with its hours).
- No check-in on a working day by `attendance.absent_after` (17:00): the system marks it ABSENT.
  A past working day with no check-in is absent the same way.
- Leave: approved leave shows as LEAVE_APPROVED, leave still waiting for the Team Lead or Project
  Manager as LEAVE_APPLIED ("applied, not approved"). Until it is approved the day still counts as
  absent in payroll; the status says so plainly.
- An approved attendance correction is what the day counts (CORRECTED); a waiting one shows as
  PENDING_APPROVAL.

Hours are check-out minus check-in, for days with both. A day still running today shows its minutes
so far separately and is not added to the totals. Weeks run Monday to Sunday."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import Connection, text

from hrmgmt.attendance_correction import live_by_day
from hrmgmt.attendance_grace import HALF_DAY_KINDS
from hrmgmt.timeutil import ist_date, parse_hhmm, to_ist

LABELS = {
    "PRESENT": "Present",
    "CHECKED_IN": "Checked in",
    "MISSING_CHECK_OUT": "No check-out",
    "PENDING_APPROVAL": "Waiting for approval",
    "HALF_DAY": "Half day (rejected)",
    "CORRECTED": "Corrected",
    "ABSENT": "Absent (marked by system)",
    "LEAVE_APPROVED": "Leave approved",
    "LEAVE_APPLIED": "Leave applied (not approved)",
    "NOT_CHECKED_IN": "Not checked in yet",
    "OFF_DAY": "Day off",
    "OFF_DAY_WORKED": "Worked on a day off",
    "UPCOMING": "",
    "NOT_JOINED": "Before joining",
}


def closed_for_check_in(at: time, values: dict[str, Any]) -> bool:
    return at >= parse_hhmm(values["attendance.absent_after"])


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


@dataclass
class Cell:
    day: date
    status: str = "UPCOMING"
    day_kind: str = "WORKING"
    holiday: str | None = None
    check_in_at: datetime | None = None
    check_out_at: datetime | None = None
    minutes: int = 0
    running_minutes: int | None = None
    leave: dict[str, Any] | None = None
    correction: dict[str, Any] | None = None
    exceptions: list[dict[str, Any]] = field(default_factory=list)
    marked_by_system: bool = False

    def view(self) -> dict[str, Any]:
        return {
            "date": self.day.isoformat(),
            "status": self.status,
            "label": LABELS.get(self.status, self.status),
            "dayKind": self.day_kind,
            "holiday": self.holiday,
            "checkInAt": self.check_in_at.isoformat() if self.check_in_at else None,
            "checkOutAt": self.check_out_at.isoformat() if self.check_out_at else None,
            "minutes": self.minutes,
            "runningMinutes": self.running_minutes,
            "leave": self.leave,
            "correction": self.correction,
            "exceptions": self.exceptions,
            "markedBySystem": self.marked_by_system,
        }


def cells(
    conn: Connection,
    employee_ids: list[str],
    first: date,
    last: date,
    *,
    now: datetime,
    values: dict[str, Any],
) -> dict[str, dict[date, Cell]]:
    """employee -> day -> Cell, for every day from first to last inclusive."""
    if not employee_ids:
        return {}
    today = ist_date(now)
    closed_today = closed_for_check_in(to_ist(now).time(), values)
    p = {"ids": employee_ids, "a": first, "b": last}
    holidays = {
        r[0]: str(r[1])
        for r in conn.execute(
            text(
                "SELECT holiday_date, name FROM hr.holiday WHERE status = 'DECLARED'"
                " AND holiday_date >= :a AND holiday_date <= :b"
            ),
            p,
        )
    }
    joined = {
        str(r[0]): r[1]
        for r in conn.execute(
            text(
                "SELECT employee_id, date_of_joining FROM hr.employee"
                " WHERE employee_id = ANY(CAST(:ids AS uuid[]))"
            ),
            p,
        )
    }
    records = {
        (str(r["employee_id"]), r["work_date"]): r
        for r in conn.execute(
            text(
                "SELECT attendance_id, employee_id, work_date, check_in_at, check_out_at"
                " FROM hr.attendance_day WHERE employee_id = ANY(CAST(:ids AS uuid[]))"
                " AND work_date >= :a AND work_date <= :b"
            ),
            p,
        ).mappings()
    }
    exceptions: dict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
    for e in conn.execute(
        text(
            "SELECT employee_id, work_date, kind, status FROM hr.attendance_exception"
            " WHERE employee_id = ANY(CAST(:ids AS uuid[])) AND work_date >= :a AND work_date <= :b"
            " ORDER BY created_at"
        ),
        p,
    ):
        exceptions[(str(e[0]), e[1])].append({"kind": e[2], "status": e[3]})
    leave: dict[tuple[str, date], dict[str, Any]] = {}
    for r in conn.execute(
        text(
            "SELECT request_id, employee_id, leave_type, from_date, to_date, half_day, status"
            " FROM hr.leave_request WHERE employee_id = ANY(CAST(:ids AS uuid[]))"
            " AND status IN ('PENDING', 'APPROVED') AND from_date <= :b AND to_date >= :a"
            " ORDER BY status = 'APPROVED'"  # an approved request wins over a waiting one
        ),
        p,
    ).mappings():
        day = max(r["from_date"], first)
        while day <= min(r["to_date"], last):
            leave[(str(r["employee_id"]), day)] = {
                "requestId": str(r["request_id"]),
                "leaveType": r["leave_type"],
                "halfDay": bool(r["half_day"]),
                "status": r["status"],
            }
            day += timedelta(days=1)
    corrections = live_by_day(conn, first, last)

    out: dict[str, dict[date, Cell]] = {}
    for eid in employee_ids:
        mine: dict[date, Cell] = {}
        day = first
        while day <= last:
            cell = Cell(day=day)
            if day.weekday() == 6:
                cell.day_kind = "SUNDAY"
            elif day in holidays:
                cell.day_kind, cell.holiday = "HOLIDAY", holidays[day]
            record = records.get((eid, day))
            cell.leave = leave.get((eid, day))
            cell.correction = corrections.get((eid, day))
            cell.exceptions = exceptions.get((eid, day), [])
            if record is not None:
                cell.check_in_at = record["check_in_at"]
                cell.check_out_at = record["check_out_at"]
            _decide(cell, joined.get(eid), today, closed_today, now)
            mine[day] = cell
            day += timedelta(days=1)
        out[eid] = mine
    return out


def _decide(
    cell: Cell, joined: date | None, today: date, closed_today: bool, now: datetime
) -> None:
    day = cell.day
    if cell.check_in_at and cell.check_out_at:
        cell.minutes = max(0, int((cell.check_out_at - cell.check_in_at).total_seconds() // 60))
    elif cell.check_in_at and day == today:
        cell.running_minutes = max(0, int((now - cell.check_in_at).total_seconds() // 60))

    if cell.check_in_at is None and joined is not None and day < joined:
        cell.status = "NOT_JOINED"
        return
    if cell.day_kind != "WORKING":
        cell.status = "OFF_DAY_WORKED" if cell.check_in_at else "OFF_DAY"
        return
    correction = cell.correction
    if correction and correction["status"] == "APPROVED":
        cell.status = "CORRECTED"
        return
    if cell.check_in_at is not None:
        statuses = {e["status"] for e in cell.exceptions if e["kind"] in HALF_DAY_KINDS}
        if "PENDING" in statuses or correction:
            cell.status = "PENDING_APPROVAL"
        elif "REJECTED" in statuses:
            cell.status = "HALF_DAY"
        elif cell.check_out_at is None:
            cell.status = "CHECKED_IN" if day == today else "MISSING_CHECK_OUT"
        else:
            cell.status = "PRESENT"
        return
    if cell.leave and not cell.leave["halfDay"]:
        cell.status = "LEAVE_APPROVED" if cell.leave["status"] == "APPROVED" else "LEAVE_APPLIED"
        return
    if correction:
        cell.status = "PENDING_APPROVAL"
        return
    if day > today:
        cell.status = (
            ("LEAVE_APPROVED" if cell.leave["status"] == "APPROVED" else "LEAVE_APPLIED")
            if cell.leave
            else "UPCOMING"
        )
        return
    if day < today or closed_today:
        cell.status = "ABSENT"
        cell.marked_by_system = True
        return
    cell.status = "NOT_CHECKED_IN"


def week_minutes(by_day: dict[date, Cell], start: date) -> int:
    return sum(
        by_day[start + timedelta(days=i)].minutes
        for i in range(7)
        if start + timedelta(days=i) in by_day
    )


def employee_calendar(
    conn: Connection, employee_id: str, month_first: date, *, now: datetime, values: dict[str, Any]
) -> dict[str, Any]:
    """The month as a calendar: whole weeks Monday to Sunday, each with its hours."""
    month_next = date(month_first.year + (month_first.month == 12), month_first.month % 12 + 1, 1)
    grid_first = week_start(month_first)
    grid_last = week_start(month_next - timedelta(days=1)) + timedelta(days=6)
    by_day = cells(conn, [employee_id], grid_first, grid_last, now=now, values=values)[employee_id]
    weeks = []
    start = grid_first
    while start <= grid_last:
        days = [by_day[start + timedelta(days=i)] for i in range(7)]
        weeks.append(
            {
                "weekStart": start.isoformat(),
                "weekEnd": (start + timedelta(days=6)).isoformat(),
                "minutes": sum(c.minutes for c in days),
                "daysWorked": sum(1 for c in days if c.minutes > 0),
                "days": [{**c.view(), "inMonth": month_first <= c.day < month_next} for c in days],
            }
        )
        start += timedelta(days=7)
    in_month = [c for c in by_day.values() if month_first <= c.day < month_next]
    counts: dict[str, int] = defaultdict(int)
    for c in in_month:
        counts[c.status] += 1
    return {
        "month": month_first.strftime("%Y-%m"),
        "absentAfter": values["attendance.absent_after"],
        "minimumHours": int(values["attendance.min_work_hours"]),
        "totals": {
            "minutes": sum(c.minutes for c in in_month),
            "daysWorked": sum(1 for c in in_month if c.minutes > 0),
            "statusCounts": dict(counts),
        },
        "weeks": weeks,
    }
