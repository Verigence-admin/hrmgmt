"""Attendance summary for a month, one row per person (product owner, 21 Nov 2026).

For SuperAdmin, the CEO and HR: everyone. For a Team Lead, Project Manager or Executive: everyone on
the projects they lead. For every employee: their own row. Each row has the month's working days
(so far and in all), days present, days absent (marked by the system), leave applied and not yet
approved, leave taken (paid, unpaid, other), caution days (off, worked, Sundays off in lieu) and
the hours clocked.

Every number is a count of the person's calendar days (hrmgmt.attendance_calendar), so the summary
always agrees with the calendar a person or their lead opens. Half days count as 0.5."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection, text

from hrmgmt import settings_store as cfg
from hrmgmt.api.attendance import _month_bounds, get_clock
from hrmgmt.api.attendance_reports import _lead_projects
from hrmgmt.api.employees import _own_employee_id
from hrmgmt.attendance_calendar import Cell, cells
from hrmgmt.authz import Authorizer
from hrmgmt.db import get_conn
from hrmgmt.errors import forbidden
from hrmgmt.principal import current_user, get_authorizer
from hrmgmt.security import HumanPrincipal
from hrmgmt.timeutil import Clock, ist_date

router = APIRouter(prefix="/hr/v1", tags=["Attendance summary"])

_ONE = Decimal(1)
_HALF = Decimal("0.5")
_ZERO = Decimal(0)
# Leave paid from a balance (legacy SICK and EARNED included); UNPAID is unpaid.
_PAID = ("PAID", "SPECIAL", "COMP_OFF", "SICK", "EARNED")
_PRESENT = ("PRESENT", "CHECKED_IN", "MISSING_CHECK_OUT", "PENDING_APPROVAL", "HALF_DAY")
_OUTSIDE = ("NOT_JOINED", "LEFT")


def _share(cell: Cell) -> Decimal:
    """How much of a working day this day is: 1, 0.5 on a half-day holiday, 0 when off."""
    if cell.day_kind != "WORKING" or cell.status in _OUTSIDE:
        return _ZERO
    return _HALF if cell.half_day_holiday else _ONE


def _leave_share(cell: Cell) -> Decimal:
    if cell.leave is None or cell.check_in_at is not None:
        return _ZERO
    share = _share(cell)
    return min(share, _HALF) if cell.leave["halfDay"] else share


def summarise(by_day: dict[date, Cell], today: date) -> dict[str, Any]:
    working = so_far = present = absent = applied = paid = unpaid = _ZERO
    caution_off = caution_worked = in_lieu = off_day_worked = 0
    minutes = 0
    for day, cell in by_day.items():
        share = _share(cell)
        working += share
        if day <= today:
            so_far += share
        minutes += cell.minutes
        if cell.status == "OFF_DAY_WORKED":
            off_day_worked += 1
        if share and (cell.status in _PRESENT or cell.status == "CORRECTED"):
            present += share
        if cell.status == "ABSENT":
            absent += share
        if cell.leave is not None:
            days = _leave_share(cell)
            if cell.leave["status"] == "PENDING":
                applied += days
            elif cell.leave["leaveType"] == "UNPAID":
                unpaid += days
            elif cell.leave["leaveType"] in _PAID:
                paid += days
        if cell.caution:
            status = cell.caution["status"]
            caution_off += status == "OFF"
            caution_worked += status == "WORKED"
            in_lieu += status == "IN_LIEU"
    return {
        "workingDays": float(working),
        "workingDaysSoFar": float(so_far),
        "presentDays": float(present),
        "absentDays": float(absent),
        "leaveAppliedDays": float(applied),
        "leaveTakenDays": float(paid + unpaid),
        "paidLeaveDays": float(paid),
        "unpaidLeaveDays": float(unpaid),
        "cautionDaysOff": caution_off,
        "cautionDaysWorked": caution_worked,
        "sundaysOffInLieu": in_lieu,
        "offDaysWorked": off_day_worked,
        "minutes": minutes,
        "averageMinutesPerDayPresent": int(minutes / present) if present else 0,
    }


def _people(
    conn: Connection, first: date, nxt: date, projects: dict[str, str | None] | None
) -> dict[str, dict[str, Any]]:
    """employee -> code, name and the projects they were on during the month (of `projects`, or
    every project when None; then people with no project at all are included as well)."""
    out: dict[str, dict[str, Any]] = {}
    for eid, code, name, pcode, pname in conn.execute(
        text(
            "SELECT e.employee_id, e.employee_code, e.full_name, a.project_code, a.project_name"
            " FROM hr.employee e JOIN hr.work_assignment a ON a.security_user_id = e.security_user_id"
            " WHERE a.project_code IS NOT NULL AND a.valid_from < :b"
            " AND (a.valid_to IS NULL OR a.valid_to > :a)"
            " AND (CAST(:p AS text[]) IS NULL OR a.project_code = ANY(CAST(:p AS text[])))"
        ),
        {"a": first, "b": nxt, "p": sorted(projects) if projects is not None else None},
    ):
        person = out.setdefault(
            str(eid), {"employeeCode": code, "employeeName": name, "projects": {}}
        )
        person["projects"][str(pcode)] = pname
    if projects is None:
        for eid, code, name in conn.execute(
            text(
                "SELECT employee_id, employee_code, full_name FROM hr.employee"
                " WHERE employment_status = 'ACTIVE'"
            )
        ):
            out.setdefault(str(eid), {"employeeCode": code, "employeeName": name, "projects": {}})
    return out


def _row(eid: str, person: dict[str, Any], by_day: dict[date, Cell], today: date) -> dict[str, Any]:
    return {
        "employeeId": eid,
        "employeeCode": person["employeeCode"],
        "employeeName": person["employeeName"],
        "projects": [
            {"projectCode": c, "projectName": n} for c, n in sorted(person["projects"].items())
        ],
        **summarise(by_day, today),
    }


def _month_days(first: date, nxt: date) -> tuple[date, date]:
    return first, nxt - timedelta(days=1)


@router.get("/attendance/summary/me")
def my_summary(
    month: Annotated[str | None, Query(max_length=7)] = None,
    user: HumanPrincipal = Depends(current_user),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Every employee: their own month in numbers."""
    now = clock()
    today = ist_date(now)
    eid = _own_employee_id(conn, user)
    first, nxt = _month_bounds(month, today)
    a, b = _month_days(first, nxt)
    person = conn.execute(
        text(
            "SELECT employee_code, full_name FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"
        ),
        {"e": eid},
    ).one()
    by_day = cells(conn, [eid], a, b, now=now, values=cfg.load_all(conn))[eid]
    row = _row(
        eid, {"employeeCode": person[0], "employeeName": person[1], "projects": {}}, by_day, today
    )
    return {"month": first.strftime("%Y-%m"), "employee": row}


@router.get("/attendance/summary/team")
def team_summary(
    month: Annotated[str | None, Query(max_length=7)] = None,
    project_code: Annotated[str | None, Query(alias="projectCode", max_length=60)] = None,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """SuperAdmin, the CEO and HR: everyone. A Team Lead, Project Manager or Executive: everyone on
    the projects they lead. One row per person for the month, with totals."""
    now = clock()
    today = ist_date(now)
    first, nxt = _month_bounds(month, today)
    a, b = _month_days(first, nxt)
    projects = _lead_projects(conn, authorizer, user, now)
    if project_code:
        if projects is not None and project_code not in projects:
            raise forbidden("You do not lead that project.")
        projects = {project_code: (projects or {}).get(project_code)}
    people = _people(conn, first, nxt, projects)
    by_person = cells(conn, sorted(people), a, b, now=now, values=cfg.load_all(conn))
    rows = [
        _row(eid, person, by_person[eid], today)
        for eid, person in people.items()
        if any(c.status not in ("NOT_JOINED", "LEFT") for c in by_person[eid].values())
    ]
    rows.sort(key=lambda r: (str(r["employeeName"]).lower(), str(r["employeeCode"])))
    seen: dict[str, str | None] = dict(projects) if projects is not None else {}
    if projects is None:
        for person in people.values():
            seen.update(person["projects"])
    totals: dict[str, float] = defaultdict(float)
    for r in rows:
        for key in (
            "presentDays",
            "absentDays",
            "leaveAppliedDays",
            "leaveTakenDays",
            "paidLeaveDays",
            "unpaidLeaveDays",
            "cautionDaysOff",
            "cautionDaysWorked",
            "sundaysOffInLieu",
            "minutes",
        ):
            totals[key] += r[key]
    return {
        "month": first.strftime("%Y-%m"),
        "employees": len(rows),
        "totals": dict(totals),
        # The projects the viewer can filter by: theirs, or every project seen this month.
        "projects": [
            {"projectCode": c, "projectName": n}
            for c, n in sorted(seen.items(), key=lambda p: str(p[1] or p[0]).lower())
        ],
        "people": rows,
    }
