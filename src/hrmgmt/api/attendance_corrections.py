"""Attendance correction requests: the employee asks, the Team Lead or Project Manager decides
(product owner, 6 Nov 2026). What a correction can be raised for, and what an approved one changes,
is described in hrmgmt.attendance_correction."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from hrmgmt.api.attendance import day_kind, get_clock
from hrmgmt.api.employees import _own_employee_id, _uuid
from hrmgmt.approvals import decides_attendance
from hrmgmt.attendance_grace import HALF_DAY_KINDS
from hrmgmt.audit import record_audit
from hrmgmt.authz import Authorizer
from hrmgmt.db import get_conn
from hrmgmt.errors import ApiError, conflict, not_found
from hrmgmt.principal import current_user, get_authorizer
from hrmgmt.security import HumanPrincipal
from hrmgmt.timeutil import Clock, ist_date

router = APIRouter(prefix="/hr/v1", tags=["Attendance corrections"])


class CorrectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    work_date: date = Field(alias="workDate")
    requested: Literal["FULL_DAY", "HALF_DAY"]
    reason: str = Field(min_length=1, max_length=500)


def _invalid(message: str) -> ApiError:
    return ApiError(422, "ATTENDANCE_CORRECTION_NOT_ALLOWED", message)


def _previous_month_start(today: date) -> date:
    first = today.replace(day=1)
    return date(first.year - (first.month == 1), (first.month - 2) % 12 + 1, 1)


def _check_day(conn: Connection, employee_id: str, day: date, today: date) -> None:
    """Refuses a day that cannot be corrected, with a reason the employee can act on."""
    if day >= today:
        raise _invalid("Only a past day can be corrected. Today, check in or check out instead.")
    if day < _previous_month_start(today):
        raise _invalid("Only days in this month or last month can be corrected.")
    kind, holiday = day_kind(conn, day)
    if kind == "SUNDAY":
        raise _invalid("A Sunday is not a working day. Work on a day off is approved separately.")
    if kind == "HOLIDAY":
        raise _invalid(f"{holiday} is a declared holiday, not a working day.")
    joined = conn.execute(
        text("SELECT date_of_joining FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"),
        {"e": employee_id},
    ).scalar_one_or_none()
    if joined is not None and day < joined:
        raise _invalid("That day is before your joining date.")
    locked = conn.execute(
        text(
            "SELECT 1 FROM hr.payroll_run WHERE pay_month = :m"
            " AND status IN ('SUBMITTED', 'APPROVED', 'PAID')"
        ),
        {"m": day.replace(day=1)},
    ).first()
    if locked:
        raise _invalid(f"Payroll for {day:%B %Y} has already been submitted. Speak to HR.")
    on_leave = conn.execute(
        text(
            "SELECT 1 FROM hr.leave_request WHERE employee_id = CAST(:e AS uuid)"
            " AND status = 'APPROVED' AND NOT half_day AND from_date <= :d AND to_date >= :d"
        ),
        {"e": employee_id, "d": day},
    ).first()
    if on_leave:
        raise _invalid("You were on approved leave that day.")
    record = (
        conn.execute(
            text(
                "SELECT attendance_id, check_in_at, check_out_at FROM hr.attendance_day"
                " WHERE employee_id = CAST(:e AS uuid) AND work_date = :d"
            ),
            {"e": employee_id, "d": day},
        )
        .mappings()
        .first()
    )
    if record is None or record["check_in_at"] is None or record["check_out_at"] is None:
        return
    statuses = {
        r[0]
        for r in conn.execute(
            text(
                "SELECT status FROM hr.attendance_exception WHERE attendance_id = CAST(:a AS uuid)"
                " AND kind = ANY(:k)"
            ),
            {"a": str(record["attendance_id"]), "k": list(HALF_DAY_KINDS)},
        )
    }
    if "PENDING" in statuses:
        raise _invalid("Your Team Lead has not decided this day yet. Wait for that decision.")
    if "REJECTED" not in statuses:
        raise _invalid("This day already counts as a full day.")


def _view(r: Any) -> dict[str, Any]:
    return {
        "correctionId": str(r["correction_id"]),
        "workDate": r["work_date"].isoformat(),
        "requested": r["requested"],
        "reason": r["reason"],
        "status": r["status"],
        "decisionNote": r["decision_note"],
        "decidedAt": r["decided_at"].isoformat() if r["decided_at"] else None,
        "createdAt": r["created_at"].isoformat(),
    }


@router.post("/attendance/corrections")
def raise_correction(
    body: CorrectionIn,
    request: Request,
    user: HumanPrincipal = Depends(current_user),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """An employee asks for a past working day to count as a full day or a half day."""
    employee_id = _own_employee_id(conn, user)
    _check_day(conn, employee_id, body.work_date, ist_date(clock()))
    live = conn.execute(
        text(
            "SELECT status FROM hr.attendance_correction WHERE employee_id = CAST(:e AS uuid)"
            " AND work_date = :d AND status IN ('PENDING', 'APPROVED') FOR UPDATE"
        ),
        {"e": employee_id, "d": body.work_date},
    ).first()
    if live is not None:
        raise conflict(
            "ATTENDANCE_CORRECTION_EXISTS",
            "A correction for this day is already waiting."
            if live[0] == "PENDING"
            else "A correction for this day has already been approved.",
        )
    row = (
        conn.execute(
            text(
                "INSERT INTO hr.attendance_correction (employee_id, work_date, requested, reason,"
                " created_at) VALUES (CAST(:e AS uuid), :d, :q, :r, :n) RETURNING *"
            ),
            {
                "e": employee_id,
                "d": body.work_date,
                "q": body.requested,
                "r": body.reason,
                "n": clock(),
            },
        )
        .mappings()
        .one()
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="ATTENDANCE_CORRECTION_RAISED",
        entity_type="employee",
        entity_id=employee_id,
        changes={"workDate": body.work_date.isoformat(), "requested": body.requested},
        request=request,
    )
    return _view(row)


@router.get("/attendance/corrections/me")
def my_corrections(
    user: HumanPrincipal = Depends(current_user),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    rows = conn.execute(
        text(
            "SELECT * FROM hr.attendance_correction WHERE employee_id = CAST(:e AS uuid)"
            " ORDER BY work_date DESC, created_at DESC LIMIT 100"
        ),
        {"e": employee_id},
    ).mappings()
    return {"items": [_view(r) for r in rows]}


@router.post("/attendance/corrections/{correction_id}/cancel")
def cancel_correction(
    correction_id: str,
    request: Request,
    user: HumanPrincipal = Depends(current_user),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    cid = _uuid(correction_id)
    row = conn.execute(
        text(
            "SELECT status FROM hr.attendance_correction WHERE correction_id = CAST(:c AS uuid)"
            " AND employee_id = CAST(:e AS uuid) FOR UPDATE"
        ),
        {"c": cid, "e": employee_id},
    ).first()
    if row is None:
        raise not_found("Request not found.")
    if row[0] != "PENDING":
        raise conflict("APPROVAL_ALREADY_DECIDED", "Only a waiting request can be withdrawn.")
    conn.execute(
        text(
            "UPDATE hr.attendance_correction SET status = 'CANCELLED'"
            " WHERE correction_id = CAST(:c AS uuid)"
        ),
        {"c": cid},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="ATTENDANCE_CORRECTION_CANCELLED",
        entity_type="employee",
        entity_id=employee_id,
        changes={"correctionId": cid},
        request=request,
    )
    return {"correctionId": cid, "status": "CANCELLED"}


# ---- approvals -----------------------------------------------------------------------------


def _day_facts(conn: Connection, employee_id: str, day: date) -> dict[str, Any]:
    """What the approver needs to judge it: what was recorded that day."""
    record = (
        conn.execute(
            text(
                "SELECT attendance_id, check_in_at, check_out_at FROM hr.attendance_day"
                " WHERE employee_id = CAST(:e AS uuid) AND work_date = :d"
            ),
            {"e": employee_id, "d": day},
        )
        .mappings()
        .first()
    )
    if record is None:
        return {"checkInAt": None, "checkOutAt": None, "exceptions": []}
    exceptions = [
        {"kind": r[0], "status": r[1]}
        for r in conn.execute(
            text(
                "SELECT kind, status FROM hr.attendance_exception"
                " WHERE attendance_id = CAST(:a AS uuid) ORDER BY created_at"
            ),
            {"a": str(record["attendance_id"])},
        )
    ]
    return {
        "checkInAt": record["check_in_at"].isoformat() if record["check_in_at"] else None,
        "checkOutAt": record["check_out_at"].isoformat() if record["check_out_at"] else None,
        "exceptions": exceptions,
    }


@router.get("/approvals/attendance-corrections")
def correction_approvals(
    status: Annotated[Literal["PENDING", "APPROVED", "REJECTED"], Query()] = "PENDING",
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Correction requests this person may decide on."""
    now = clock()
    rows = (
        conn.execute(
            text(
                "SELECT c.*, e.full_name, e.employee_code FROM hr.attendance_correction c"
                " JOIN hr.employee e ON e.employee_id = c.employee_id WHERE c.status = :s"
                " ORDER BY c.work_date DESC, c.created_at LIMIT 300"
            ),
            {"s": status},
        )
        .mappings()
        .all()
    )
    decides: dict[str, bool] = {}
    items = []
    for r in rows:
        eid = str(r["employee_id"])
        if eid not in decides:
            decides[eid] = decides_attendance(conn, authorizer, user, eid, now)
        if not decides[eid]:
            continue
        items.append(
            {
                **_view(r),
                "employeeId": eid,
                "employeeName": r["full_name"],
                "employeeCode": r["employee_code"],
                "day": _day_facts(conn, eid, r["work_date"]),
            }
        )
    return {"items": items}


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["APPROVE", "REJECT"]
    note: str | None = Field(default=None, max_length=500)


@router.post("/approvals/attendance-corrections/{correction_id}/decision")
def decide_correction(
    correction_id: str,
    body: Decision,
    request: Request,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    cid = _uuid(correction_id)
    row = (
        conn.execute(
            text(
                "SELECT employee_id, status, work_date, requested FROM hr.attendance_correction"
                " WHERE correction_id = CAST(:c AS uuid) FOR UPDATE"
            ),
            {"c": cid},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Request not found.")
    employee_id = str(row["employee_id"])
    now: datetime = clock()
    if not decides_attendance(conn, authorizer, user, employee_id, now, hr_override=True):
        raise not_found("Request not found.")
    if row["status"] != "PENDING":
        raise conflict("APPROVAL_ALREADY_DECIDED", "This request has already been decided.")
    if body.decision == "REJECT" and not (body.note and body.note.strip()):
        raise ApiError(422, "APPROVAL_NOTE_REQUIRED", "Say why you are rejecting it.")
    status = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
    conn.execute(
        text(
            "UPDATE hr.attendance_correction SET status = :s, decided_by = :u, decided_at = :n,"
            " decision_note = :note WHERE correction_id = CAST(:c AS uuid)"
        ),
        {
            "s": status,
            "u": user.user_id,
            "n": now,
            "note": (body.note or "").strip() or None,
            "c": cid,
        },
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="ATTENDANCE_CORRECTION_" + status,
        entity_type="employee",
        entity_id=employee_id,
        changes={
            "correctionId": cid,
            "workDate": row["work_date"].isoformat(),
            "requested": row["requested"],
        },
        request=request,
    )
    return {"correctionId": cid, "status": status}
