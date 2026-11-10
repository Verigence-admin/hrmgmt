"""Attendance correction requests: the employee asks, the Team Lead or Project Manager decides
(product owner, 6 Nov 2026). What a correction can be raised for, and what an approved one changes,
is described in hrmgmt.attendance_correction."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from hrmgmt import holidays as hol
from hrmgmt import settings_store as cfg
from hrmgmt.api.attendance import day_kind, get_clock
from hrmgmt.api.employees import _own_employee_id, _uuid
from hrmgmt.approvals import decides_attendance
from hrmgmt.attendance_correction import CHECK_OUT, CORRECTED_CHECK_OUT_FLAG
from hrmgmt.attendance_grace import HALF_DAY_KINDS
from hrmgmt.audit import record_audit
from hrmgmt.authz import Authorizer
from hrmgmt.db import get_conn
from hrmgmt.errors import ApiError, conflict, not_found
from hrmgmt.principal import current_user, get_authorizer
from hrmgmt.security import HumanPrincipal
from hrmgmt.timeutil import IST, Clock, ist_date

router = APIRouter(prefix="/hr/v1", tags=["Attendance corrections"])


class CorrectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    work_date: date = Field(alias="workDate")
    requested: Literal["FULL_DAY", "HALF_DAY", "CHECK_OUT"]
    reason: str = Field(min_length=1, max_length=500)
    # CHECK_OUT only: the time the person left that day, IST, "HH:MM".
    check_out_time: time | None = Field(default=None, alias="checkOutTime")


def _invalid(message: str) -> ApiError:
    return ApiError(422, "ATTENDANCE_CORRECTION_NOT_ALLOWED", message)


def _month_start(today: date) -> date:
    return today.replace(day=1)


def _check_day(
    conn: Connection, employee_id: str, day: date, today: date, closed_today: bool = False
) -> None:
    """Refuses a day that cannot be corrected, with a reason the employee can act on. Today can be
    corrected once it is past 17:00 and the system has marked it absent."""
    if day > today or (day == today and not closed_today):
        raise _invalid("Only a past day can be corrected. Today, check in or check out instead.")
    # Product owner (7 Nov 2026): a correction can be raised only for a day of the current calendar
    # month; a day of an earlier month cannot be raised any more.
    if day < _month_start(today):
        raise _invalid(
            f"Only days in {today:%B %Y} can be corrected. A day of an earlier month cannot be raised."
        )
    kind, holiday = day_kind(conn, day, employee_id)
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


def _check_out_moment(
    conn: Connection, employee_id: str, day: date, left_at: time | None, now: datetime
) -> datetime:
    """A missed check-out (product owner, 10 Nov 2026): the day has a check-in and no check-out --
    today, or an earlier day of this month -- and the time given is after the check-in, on that
    day, and not later than now. Returns the moment to record."""
    if left_at is None:
        raise _invalid("Give the time you left.")
    today = ist_date(now)
    if day > today:
        raise _invalid("A check-out can only be recorded for today or an earlier day.")
    if day < _month_start(today):
        raise _invalid(
            f"Only days in {today:%B %Y} can be corrected. A day of an earlier month cannot be raised."
        )
    locked = conn.execute(
        text(
            "SELECT 1 FROM hr.payroll_run WHERE pay_month = :m"
            " AND status IN ('SUBMITTED', 'APPROVED', 'PAID')"
        ),
        {"m": day.replace(day=1)},
    ).first()
    if locked:
        raise _invalid(f"Payroll for {day:%B %Y} has already been submitted. Speak to HR.")
    record = (
        conn.execute(
            text(
                "SELECT check_in_at, check_out_at FROM hr.attendance_day"
                " WHERE employee_id = CAST(:e AS uuid) AND work_date = :d"
            ),
            {"e": employee_id, "d": day},
        )
        .mappings()
        .first()
    )
    if record is None or record["check_in_at"] is None:
        raise _invalid(
            "There is no check-in on that day, so there is no check-out to record. Ask for the day"
            " to count as a full day or a half day instead."
        )
    if record["check_out_at"] is not None:
        raise _invalid("You have already checked out that day.")
    moment = datetime.combine(day, left_at.replace(second=0, microsecond=0), tzinfo=IST)
    if moment <= record["check_in_at"]:
        raise _invalid(
            f"The time you left must be after your check-in at "
            f"{record['check_in_at'].astimezone(IST):%H:%M}."
        )
    if moment > now + timedelta(minutes=1):
        raise _invalid("The time you left cannot be later than now.")
    return moment


def _closed_today(conn: Connection, employee_id: str, now: datetime) -> bool:
    """Past the time after which today is marked absent: 17:00, or the half-day split when this
    person's afternoon is a holiday today."""
    half = hol.half_day(ist_date(now), hol.holiday_for(conn, employee_id, ist_date(now)))
    return hol.closed_for_check_in(now, half.day_part if half else None, cfg.load_all(conn))


def _view(r: Any) -> dict[str, Any]:
    return {
        "correctionId": str(r["correction_id"]),
        "workDate": r["work_date"].isoformat(),
        "requested": r["requested"],
        "reason": r["reason"],
        "status": r["status"],
        "checkOutAt": r["check_out_at"].isoformat() if r["check_out_at"] else None,
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
    now = clock()
    left_at = None
    if body.requested == CHECK_OUT:
        left_at = _check_out_moment(conn, employee_id, body.work_date, body.check_out_time, now)
    else:
        _check_day(
            conn,
            employee_id,
            body.work_date,
            ist_date(now),
            _closed_today(conn, employee_id, now),
        )
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
                " check_out_at, created_at) VALUES (CAST(:e AS uuid), :d, :q, :r, :o, :n)"
                " RETURNING *"
            ),
            {
                "e": employee_id,
                "d": body.work_date,
                "q": body.requested,
                "r": body.reason,
                "o": left_at,
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
        changes={
            "workDate": body.work_date.isoformat(),
            "requested": body.requested,
            "checkOutAt": left_at.isoformat() if left_at else None,
        },
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
                "SELECT employee_id, status, work_date, requested, check_out_at"
                " FROM hr.attendance_correction WHERE correction_id = CAST(:c AS uuid) FOR UPDATE"
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
    if status == "APPROVED" and row["requested"] == CHECK_OUT:
        # The check-out is written to the day at the time asked, flagged as recorded from this
        # approved correction. If the person has checked out since, there is nothing to record.
        written = conn.execute(
            text(
                "UPDATE hr.attendance_day SET check_out_at = :o, updated_at = :n,"
                " check_out_flags = CAST(:f AS jsonb)"
                " WHERE employee_id = CAST(:e AS uuid) AND work_date = :d"
                " AND check_in_at IS NOT NULL AND check_in_at < :o AND check_out_at IS NULL"
            ),
            {
                "o": row["check_out_at"],
                "n": now,
                "f": f'["{CORRECTED_CHECK_OUT_FLAG}"]',
                "e": employee_id,
                "d": row["work_date"],
            },
        ).rowcount
        if not written:
            raise conflict(
                "ATTENDANCE_ALREADY_CHECKED_OUT",
                "This day already has a check-out, so there is nothing to record. Reject it, or ask"
                " the employee to withdraw it.",
            )
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
            "checkOutAt": row["check_out_at"].isoformat() if row["check_out_at"] else None,
        },
        request=request,
    )
    return {"correctionId": cid, "status": status}
