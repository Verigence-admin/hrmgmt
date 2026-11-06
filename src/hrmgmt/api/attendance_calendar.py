"""Hours clocked and the attendance calendar (product owner, 6 Nov 2026).

- Every employee sees their own month as a calendar: each day's status (present, absent marked by
  the system at 17:00, leave applied or approved, corrected, day off), hours per day and per week.
- A Team Lead, Project Manager or Executive sees the same calendar for anyone on a project they
  lead; HR and the CEO for anyone.
- The weekly view: one week (Monday to Sunday) of every person on the projects the viewer leads,
  project by project, with each day's status and hours and the week's total. A person on more than
  one project that week is marked as shared and named under each project they are on.

Day statuses and hours come from hrmgmt.attendance_calendar."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection, text

from hrmgmt import settings_store as cfg
from hrmgmt.api.attendance import _month_bounds, get_clock
from hrmgmt.api.attendance_reports import _closed, _day, _lead_projects
from hrmgmt.api.employees import _own_employee_id, _uuid
from hrmgmt.attendance_calendar import LABELS, cells, employee_calendar, week_start
from hrmgmt.attendance_report import build_rows
from hrmgmt.authz import Authorizer
from hrmgmt.db import get_conn
from hrmgmt.errors import forbidden, not_found
from hrmgmt.principal import current_user, get_authorizer
from hrmgmt.security import HumanPrincipal
from hrmgmt.timeutil import Clock, ist_date

router = APIRouter(prefix="/hr/v1", tags=["Attendance calendar"])


@router.get("/attendance/calendar/me")
def my_calendar(
    month: Annotated[str | None, Query(max_length=7)] = None,
    user: HumanPrincipal = Depends(current_user),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    now = clock()
    employee_id = _own_employee_id(conn, user)
    first, _ = _month_bounds(month, ist_date(now))
    return employee_calendar(conn, employee_id, first, now=now, values=cfg.load_all(conn))


def _projects_of(conn: Connection, employee_id: str, first: date, last: date) -> set[str]:
    return {
        str(r[0])
        for r in conn.execute(
            text(
                "SELECT DISTINCT a.project_code FROM hr.work_assignment a"
                " JOIN hr.employee e ON e.security_user_id = a.security_user_id"
                " WHERE e.employee_id = CAST(:e AS uuid) AND a.project_code IS NOT NULL"
                " AND a.valid_from < :b AND (a.valid_to IS NULL OR a.valid_to > :a)"
            ),
            {"e": employee_id, "a": first, "b": last + timedelta(days=1)},
        )
    }


@router.get("/attendance/calendar/employee/{employee_id}")
def employee_calendar_view(
    employee_id: str,
    month: Annotated[str | None, Query(max_length=7)] = None,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """A lead (for people on their projects), HR or the CEO: one person's calendar."""
    now = clock()
    eid = _uuid(employee_id)
    person = conn.execute(
        text(
            "SELECT employee_code, full_name FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"
        ),
        {"e": eid},
    ).first()
    if person is None:
        raise not_found("Employee not found.")
    first, nxt = _month_bounds(month, ist_date(now))
    projects = _lead_projects(conn, authorizer, user, now)
    if projects is not None and not (_projects_of(conn, eid, first, nxt) & projects.keys()):
        raise not_found("Employee not found.")
    body = employee_calendar(conn, eid, first, now=now, values=cfg.load_all(conn))
    return {**body, "employeeId": eid, "employeeCode": person[0], "employeeName": person[1]}


@router.get("/attendance/team-weekly")
def team_weekly(
    on: Annotated[str | None, Query(alias="date", max_length=10)] = None,
    project_code: Annotated[str | None, Query(alias="projectCode", max_length=60)] = None,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """The week (Monday to Sunday) holding `date`, for every person on the viewer's projects,
    project by project: each day's status and hours, and the week's total hours."""
    now = clock()
    today = ist_date(now)
    start = week_start(_day(on, today, "Date"))
    end = start + timedelta(days=6)
    projects = _lead_projects(conn, authorizer, user, now)
    if project_code and projects is not None and project_code not in projects:
        raise forbidden("You do not lead that project.")
    # Who was on which project that week: everyone with an assignment on any day of it.
    members: dict[tuple[str, str | None], dict[str, Any]] = {}
    on_projects: dict[str, dict[str | None, str | None]] = defaultdict(dict)
    for r in build_rows(conn, start, min(end, today), today=today, closed_today=_closed(conn, now)):
        on_projects[r.employee_id][r.project_code] = r.project_name
        key = (r.employee_id, r.project_code)
        entry = members.setdefault(
            key,
            {
                "employeeId": r.employee_id,
                "employeeCode": r.employee_code,
                "employeeName": r.employee_name,
                "projectCode": r.project_code,
                "projectName": r.project_name,
                "roles": [],
                "outlets": [],
            },
        )
        entry["roles"] = sorted(set(entry["roles"]) | set(r.roles))
        entry["outlets"] = sorted(set(entry["outlets"]) | set(r.outlets))
    visible = [
        m
        for m in members.values()
        if (projects is None or m["projectCode"] in projects)
        and (not project_code or m["projectCode"] == project_code)
    ]
    by_person = cells(
        conn,
        sorted({m["employeeId"] for m in visible}),
        start,
        end,
        now=now,
        values=cfg.load_all(conn),
    )
    days = [start + timedelta(days=i) for i in range(7)]
    grouped: dict[tuple[str | None, str | None], list[dict[str, Any]]] = defaultdict(list)
    for m in visible:
        mine = by_person[m["employeeId"]]
        others = [
            {"projectCode": c, "projectName": n}
            for c, n in on_projects[m["employeeId"]].items()
            if c and c != m["projectCode"]
        ]
        grouped[(m["projectCode"], m["projectName"])].append(
            {
                **m,
                "shared": bool(others),
                "otherProjects": others,
                "weekMinutes": sum(mine[d].minutes for d in days),
                "daysWorked": sum(1 for d in days if mine[d].minutes > 0),
                "days": [mine[d].view() for d in days],
            }
        )
    out: list[dict[str, Any]] = []
    everyone: dict[str, dict[str, Any]] = {}
    for (code, name), people in sorted(
        grouped.items(), key=lambda g: str(g[0][1] or g[0][0] or "")
    ):
        people.sort(key=lambda p: (str(p["employeeName"]).lower(), p["employeeCode"]))
        for person in people:
            everyone.setdefault(person["employeeId"], person)
        out.append(
            {
                "projectCode": code,
                "projectName": name,
                "people": people,
                "summary": _week_summary(people, days),
            }
        )
    return {
        "weekStart": start.isoformat(),
        "weekEnd": end.isoformat(),
        "days": [d.isoformat() for d in days],
        "labels": LABELS,
        "summary": _week_summary(list(everyone.values()), days),
        "projects": out,
    }


def _week_summary(people: list[dict[str, Any]], days: list[date]) -> dict[str, Any]:
    def count(status: str) -> int:
        return sum(1 for p in people for c in p["days"] if c["status"] == status)

    return {
        "employees": len(people),
        "shared": sum(1 for p in people if p["shared"]),
        "minutes": sum(p["weekMinutes"] for p in people),
        "absentDays": count("ABSENT"),
        "leaveApprovedDays": count("LEAVE_APPROVED"),
        "leaveAppliedDays": count("LEAVE_APPLIED"),
        "pendingDays": count("PENDING_APPROVAL"),
        "byDay": [
            {
                "date": d.isoformat(),
                "present": sum(1 for p in people if p["days"][i]["checkInAt"]),
                "absent": sum(1 for p in people if p["days"][i]["status"] == "ABSENT"),
                "onLeave": sum(1 for p in people if p["days"][i]["status"] == "LEAVE_APPROVED"),
                "minutes": sum(p["days"][i]["minutes"] for p in people),
            }
            for i, d in enumerate(days)
        ],
    }
