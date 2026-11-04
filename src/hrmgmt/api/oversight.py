"""What is waiting with each Team Lead, and since when (product owner, 4 Nov 2026).

An Executive sees the Team Leads and Project Managers of the projects where they are the Executive;
HR (whoever reads all attendance) and the CEO see every project. For each approver: what is
pending with them (attendance exceptions, leave, reimbursement claims at the Team Lead step), the
oldest, how many are past the deadline, and how quickly they decided over the last 30 days.

Deadline: whatever is raised in a week (Monday to Sunday, IST) is to be decided by 09:00 IST the
following Monday. An item is waiting with every Team Lead or Project Manager who may decide it, so
one item on a project with two Team Leads shows against both."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection, text

from hrmgmt import permissions as perm
from hrmgmt.api.attendance import get_clock
from hrmgmt.approvals import is_ceo
from hrmgmt.authz import Authorizer
from hrmgmt.db import get_conn
from hrmgmt.errors import forbidden
from hrmgmt.principal import current_user, get_authorizer, has_permission
from hrmgmt.security import HumanPrincipal
from hrmgmt.timeutil import IST, Clock, to_ist

router = APIRouter(prefix="/hr/v1", tags=["Oversight"])

_APPROVER_ROLES = ("TL", "PM")
_WINDOW_DAYS = 30


def deadline_for(raised_at: datetime) -> datetime:
    """09:00 IST on the Monday after the week (Monday to Sunday, IST) it was raised in."""
    local = to_ist(raised_at)
    monday = local.date() - timedelta(days=local.weekday())
    return datetime.combine(monday + timedelta(days=7), time(9, 0), tzinfo=IST)


def _scope(
    conn: Connection, authorizer: Authorizer, user: HumanPrincipal, now: datetime
) -> list[str] | None:
    """The projects (tenants) this person oversees; None means every project."""
    if is_ceo(authorizer, user.user_id) or has_permission(
        authorizer, user, perm.HR_ATTENDANCE_READ_ALL
    ):
        return None
    tenants = [
        r[0]
        for r in conn.execute(
            text(
                "SELECT DISTINCT tenant_id FROM hr.work_assignment"
                " WHERE security_user_id = CAST(:u AS uuid) AND role_code = 'EXECUTIVE'"
                " AND valid_from <= :n AND (valid_to IS NULL OR valid_to > :n)"
            ),
            {"u": user.user_id, "n": now},
        )
    ]
    if not tenants:
        raise forbidden("Only an Executive, HR or the CEO can see this.")
    return tenants


def _current_assignments(conn: Connection, now: datetime) -> list[Any]:
    return list(
        conn.execute(
            text(
                "SELECT security_user_id, tenant_id, project_name, role_code FROM hr.work_assignment"
                " WHERE valid_from <= :n AND (valid_to IS NULL OR valid_to > :n)"
            ),
            {"n": now},
        )
    )


def _pending_items(conn: Connection) -> list[dict[str, Any]]:
    """Everything a Team Lead or Project Manager decides that is still waiting."""
    items: list[dict[str, Any]] = []
    for r in conn.execute(
        text(
            """
            SELECT x.exception_id, x.kind, x.work_date, x.created_at, e.security_user_id,
                   e.full_name, e.employee_code
            FROM hr.attendance_exception x JOIN hr.employee e ON e.employee_id = x.employee_id
            WHERE x.status = 'PENDING' AND x.kind <> 'NO_OUTLET_LOCATION'
            """
        )
    ):
        items.append(
            {
                "type": "ATTENDANCE",
                "id": str(r[0]),
                "detail": r[1],
                "forDate": r[2].isoformat(),
                "raisedAt": r[3],
                "employeeUser": str(r[4]) if r[4] else None,
                "employeeName": r[5],
                "employeeCode": r[6],
                "roles": _APPROVER_ROLES,
            }
        )
    for r in conn.execute(
        text(
            """
            SELECT q.request_id, q.leave_type, q.from_date, q.submitted_at, e.security_user_id,
                   e.full_name, e.employee_code, q.approver_rule
            FROM hr.leave_request q JOIN hr.employee e ON e.employee_id = q.employee_id
            WHERE q.status = 'PENDING' AND q.approver_rule IN ('TL_PM', 'PM')
            """
        )
    ):
        items.append(
            {
                "type": "LEAVE",
                "id": str(r[0]),
                "detail": r[1],
                "forDate": r[2].isoformat(),
                "raisedAt": r[3],
                "employeeUser": str(r[4]) if r[4] else None,
                "employeeName": r[5],
                "employeeCode": r[6],
                "roles": ("PM",) if r[7] == "PM" else _APPROVER_ROLES,
            }
        )
    for r in conn.execute(
        text(
            """
            SELECT c.claim_id, c.category_code, c.expense_date, c.updated_at, e.security_user_id,
                   e.full_name, e.employee_code
            FROM hr.claim c JOIN hr.employee e ON e.employee_id = c.employee_id
            WHERE c.status = 'SUBMITTED' AND c.stage_plan[c.stage_index + 1] = 'TL_PM'
            """
        )
    ):
        items.append(
            {
                "type": "CLAIM",
                "id": str(r[0]),
                "detail": r[1],
                "forDate": r[2].isoformat(),
                "raisedAt": r[3],
                "employeeUser": str(r[4]) if r[4] else None,
                "employeeName": r[5],
                "employeeCode": r[6],
                "roles": _APPROVER_ROLES,
            }
        )
    return items


def _decision_times(conn: Connection, since: datetime) -> dict[str, list[float]]:
    """Hours from raised to decided, per decider, over the window."""
    out: dict[str, list[float]] = defaultdict(list)
    for who, raised, decided in conn.execute(
        text(
            "SELECT decided_by, created_at, decided_at FROM hr.attendance_exception"
            " WHERE decided_at >= :s AND decided_by IS NOT NULL"
            " UNION ALL"
            " SELECT decided_by, submitted_at, decided_at FROM hr.leave_request"
            " WHERE decided_at >= :s AND decided_by IS NOT NULL AND approver_rule IN ('TL_PM', 'PM')"
            " UNION ALL"
            " SELECT ev.actor, c.submitted_at, ev.occurred_at FROM hr.claim_event ev"
            " JOIN hr.claim c ON c.claim_id = ev.claim_id"
            " WHERE ev.occurred_at >= :s AND ev.stage = 'TL_PM' AND ev.actor IS NOT NULL"
            " AND ev.event_type IN ('APPROVED', 'STAGE_APPROVED', 'REJECTED', 'CORRECTION_REQUESTED')"
        ),
        {"s": since},
    ):
        out[str(who)].append(max(0.0, (decided - raised).total_seconds() / 3600))
    return out


def _build(
    conn: Connection, scope: list[str] | None, now: datetime
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    assignments = _current_assignments(conn, now)
    in_scope = [a for a in assignments if scope is None or a[1] in scope]
    # employee -> their tenants (in scope); tenant -> approvers by role
    tenants_of: dict[str, set[str]] = defaultdict(set)
    approvers: dict[tuple[str, str], set[str]] = defaultdict(set)
    approver_roles: dict[str, list[dict[str, str]]] = defaultdict(list)
    for user_id, tenant, project, role in in_scope:
        uid = str(user_id)
        tenants_of[uid].add(tenant)
        if role in _APPROVER_ROLES:
            approvers[(tenant, role)].add(uid)
            approver_roles[uid].append({"project": project, "role": role})
    names = {
        str(r[0]): r[1]
        for r in conn.execute(
            text(
                "SELECT security_user_id, full_name FROM hr.employee"
                " WHERE security_user_id IS NOT NULL"
            )
        )
    }
    rows: dict[str, dict[str, Any]] = {}
    for uid, roles in approver_roles.items():
        rows[uid] = {
            "userId": uid,
            "name": names.get(uid, "Not an employee in myHR"),
            "roles": roles,
            "pending": {"ATTENDANCE": 0, "LEAVE": 0, "CLAIM": 0, "total": 0},
            "oldestRaisedAt": None,
            "overdue": 0,
        }
    unassigned: list[dict[str, Any]] = []
    by_approver: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in _pending_items(conn):
        user = item["employeeUser"]
        tenants = tenants_of.get(user, set()) if user else set()
        if scope is not None and not tenants:
            continue  # someone outside the projects this person oversees
        deciders = {
            a
            for t in tenants
            for role in item["roles"]
            for a in approvers.get((t, role), set())
            if a != user
        }
        deadline = deadline_for(item["raisedAt"])
        view = {
            "type": item["type"],
            "id": item["id"],
            "detail": item["detail"],
            "forDate": item["forDate"],
            "raisedAt": item["raisedAt"].isoformat(),
            "deadline": deadline.isoformat(),
            "overdue": now > deadline,
            "ageHours": round((now - item["raisedAt"]).total_seconds() / 3600, 1),
            "employeeName": item["employeeName"],
            "employeeCode": item["employeeCode"],
        }
        if not deciders:
            unassigned.append(view)
            continue
        for uid in deciders:
            row = rows[uid]
            row["pending"][item["type"]] += 1
            row["pending"]["total"] += 1
            if row["oldestRaisedAt"] is None or item["raisedAt"] < row["oldestRaisedAt"]:
                row["oldestRaisedAt"] = item["raisedAt"]
            if view["overdue"]:
                row["overdue"] += 1
            by_approver[uid].append(view)
    return rows, unassigned, by_approver


@router.get("/oversight/approvers")
def approvers_overview(
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    now = clock()
    scope = _scope(conn, authorizer, user, now)
    rows, unassigned, _ = _build(conn, scope, now)
    times = _decision_times(conn, now - timedelta(days=_WINDOW_DAYS))
    items = []
    for uid, row in rows.items():
        decided = times.get(uid, [])
        oldest = row.pop("oldestRaisedAt")
        items.append(
            {
                **row,
                "oldestRaisedAt": oldest.isoformat() if oldest else None,
                "oldestAgeHours": round((now - oldest).total_seconds() / 3600, 1)
                if oldest
                else None,
                "decidedLast30Days": len(decided),
                "averageDecisionHours": round(sum(decided) / len(decided), 1) if decided else None,
            }
        )
    items.sort(key=lambda r: (-r["overdue"], -r["pending"]["total"], r["name"].lower()))
    return {
        "asOf": now.isoformat(),
        "scope": "ALL" if scope is None else "PROJECTS",
        "items": items,
        "withoutApprover": unassigned,
    }


@router.get("/oversight/approvers/{approver_user_id}/pending")
def approver_pending(
    approver_user_id: str,
    only_overdue: Annotated[bool, Query()] = False,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    now = clock()
    scope = _scope(conn, authorizer, user, now)
    rows, _, by_approver = _build(conn, scope, now)
    if approver_user_id not in rows:
        raise forbidden("This person is not a Team Lead or Project Manager you oversee.")
    pending = sorted(by_approver.get(approver_user_id, []), key=lambda v: v["raisedAt"])
    if only_overdue:
        pending = [p for p in pending if p["overdue"]]
    return {"approver": rows[approver_user_id]["name"], "items": pending}
