"""Attendance correction requests: the employee asks, the Team Lead or Project Manager decides
(product owner, 6 Nov 2026). What a correction can be raised for, and what an approved one changes,
is described in hrmgmt.attendance_correction."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from hrmgmt import attendance_month as am
from hrmgmt import holidays as hol
from hrmgmt import leave_rules as lr
from hrmgmt import settings_store as cfg
from hrmgmt import short_day
from hrmgmt.api.attendance import day_kind, get_clock
from hrmgmt.api.employees import _own_employee_id, _uuid
from hrmgmt.approvals import decides_attendance
from hrmgmt.attendance_calendar import weekly_hours
from hrmgmt.attendance_correction import (
    ADJUST,
    CHECK_OUT,
    CORRECTED_CHECK_OUT_FLAG,
    CORRECTED_FLAG,
    LOCATION_CORRECTED_FLAG,
)
from hrmgmt.attendance_grace import HALF_DAY_KINDS
from hrmgmt.audit import record_audit
from hrmgmt.authz import Authorizer
from hrmgmt.db import get_conn
from hrmgmt.errors import ApiError, conflict, not_found
from hrmgmt.principal import current_user, get_authorizer
from hrmgmt.security import HumanPrincipal
from hrmgmt.timeutil import IST, Clock, ist_date, parse_hhmm, to_ist

router = APIRouter(prefix="/hr/v1", tags=["Attendance corrections"])


class CorrectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    work_date: date = Field(alias="workDate")
    requested: Literal["FULL_DAY", "HALF_DAY", "CHECK_OUT", "ADJUST"]
    reason: str = Field(min_length=1, max_length=500)
    # CHECK_OUT, and ADJUST: the time the person left that day, IST, "HH:MM".
    check_out_time: time | None = Field(default=None, alias="checkOutTime")
    # ADJUST only: the time the person arrived, IST, "HH:MM", and which locations were at the
    # outlet after all.
    check_in_time: time | None = Field(default=None, alias="checkInTime")
    location_check_in: bool = Field(default=False, alias="locationCheckIn")
    location_check_out: bool = Field(default=False, alias="locationCheckOut")


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


def _payroll_locked(conn: Connection, day: date) -> bool:
    return (
        conn.execute(
            text(
                "SELECT 1 FROM hr.payroll_run WHERE pay_month = :m"
                " AND status IN ('SUBMITTED', 'APPROVED', 'PAID')"
            ),
            {"m": day.replace(day=1)},
        ).first()
        is not None
    )


def _adjust_parts(
    conn: Connection, employee_id: str, day: date, body: CorrectionIn, now: datetime
) -> tuple[datetime | None, datetime | None]:
    """A time or location correction (product owner, 16 Nov 2026): the day has a check-in, is today
    or an earlier day of this month, and payroll has not gone past draft. The check-in can only
    move earlier, the check-out only later (or be recorded when it is missing), and a location can
    be corrected only where it was outside the outlet's fence. Returns the moments to record."""
    today = ist_date(now)
    if day > today:
        raise _invalid("Only today or an earlier day can be corrected.")
    if day < _month_start(today):
        raise _invalid(
            f"Only days in {today:%B %Y} can be corrected. A day of an earlier month cannot be raised."
        )
    if _payroll_locked(conn, day):
        raise _invalid(f"Payroll for {day:%B %Y} has already been submitted. Speak to HR.")
    if not (
        body.check_in_time
        or body.check_out_time
        or body.location_check_in
        or body.location_check_out
    ):
        raise _invalid("Say what to correct: the check-in time, the check-out time or a location.")
    record = (
        conn.execute(
            text(
                "SELECT check_in_at, check_out_at, check_in_flags, check_out_flags"
                " FROM hr.attendance_day WHERE employee_id = CAST(:e AS uuid) AND work_date = :d"
            ),
            {"e": employee_id, "d": day},
        )
        .mappings()
        .first()
    )
    if record is None or record["check_in_at"] is None:
        raise _invalid(
            "There is no check-in on that day, so there is no time or location to correct. Ask for"
            " the day to count as a full day or a half day instead."
        )
    recorded_in: datetime = record["check_in_at"]
    recorded_out: datetime | None = record["check_out_at"]
    new_in = new_out = None
    if body.check_in_time is not None:
        new_in = datetime.combine(
            day, body.check_in_time.replace(second=0, microsecond=0), tzinfo=IST
        )
        if new_in >= recorded_in.replace(second=0, microsecond=0):
            raise _invalid(
                f"The check-in can only be corrected to a time before the recorded one"
                f" ({to_ist(recorded_in):%H:%M})."
            )
    if body.check_out_time is not None:
        new_out = datetime.combine(
            day, body.check_out_time.replace(second=0, microsecond=0), tzinfo=IST
        )
        if recorded_out is not None and new_out <= recorded_out:
            raise _invalid(
                f"The check-out can only be corrected to a time after the recorded one"
                f" ({to_ist(recorded_out):%H:%M})."
            )
        if new_out <= (new_in or recorded_in):
            raise _invalid(
                f"The time you left must be after your check-in at "
                f"{to_ist(new_in or recorded_in):%H:%M}."
            )
        if new_out > now + timedelta(minutes=1):
            raise _invalid("The time you left cannot be later than now.")
    if body.location_check_in and "OUT_OF_FENCE" not in (record["check_in_flags"] or []):
        raise _invalid("Your check-in location that day was not outside your outlet.")
    if body.location_check_out and (
        recorded_out is None or "OUT_OF_FENCE" not in (record["check_out_flags"] or [])
    ):
        raise _invalid("Your check-out location that day was not outside your outlet.")
    return new_in, new_out


def _apply_adjust(
    conn: Connection, row: Any, employee_id: str, now: datetime, decided_by: str
) -> dict[str, Any]:
    """Writes an approved time or location correction to the day, and resolves the exceptions it
    answers. Refuses (nothing written) when the day has changed so the correction no longer fits.
    Returns what the day had before, for the correction's history."""
    day = (
        conn.execute(
            text(
                "SELECT attendance_id, check_in_at, check_out_at, check_in_flags, check_out_flags"
                " FROM hr.attendance_day WHERE employee_id = CAST(:e AS uuid) AND work_date = :d"
                " FOR UPDATE"
            ),
            {"e": employee_id, "d": row["work_date"]},
        )
        .mappings()
        .first()
    )
    if day is None or day["check_in_at"] is None:
        raise conflict("ATTENDANCE_CORRECTION_STALE", "The day has no check-in any more.")
    new_in: datetime | None = row["check_in_at"]
    new_out: datetime | None = row["check_out_at"]
    if new_in is not None and new_in >= day["check_in_at"]:
        raise conflict(
            "ATTENDANCE_CORRECTION_STALE",
            "The check-in on the day is already at or before the time asked. Reject it, or ask the"
            " employee to withdraw it.",
        )
    if new_out is not None and day["check_out_at"] is not None and new_out <= day["check_out_at"]:
        raise conflict(
            "ATTENDANCE_ALREADY_CHECKED_OUT",
            "The check-out on the day is already at or after the time asked. Reject it, or ask the"
            " employee to withdraw it.",
        )
    in_flags = list(day["check_in_flags"] or [])
    out_flags = list(day["check_out_flags"] or [])
    work_date: date = row["work_date"]
    holiday = hol.holiday_for(conn, employee_id, work_date)
    half = hol.half_day(work_date, holiday)
    off_day = hol.is_off_day(work_date, holiday)
    values = hol.effective_values(cfg.load_all(conn), half.day_part if half else None)
    check_in_at = new_in or day["check_in_at"]
    check_out_at = new_out or day["check_out_at"]
    resolved: list[tuple[str, str | None]] = []  # (kind, event or None for both)
    if new_in is not None:
        if CORRECTED_FLAG not in in_flags:
            in_flags.append(CORRECTED_FLAG)
        if to_ist(new_in).time() <= parse_hhmm(values["attendance.late_after"]):
            in_flags = [f for f in in_flags if f != "LATE"]
            resolved.append(("LATE_CHECK_IN", None))
    if new_out is not None:
        if day["check_out_at"] is None:
            out_flags = [CORRECTED_FLAG]
        elif CORRECTED_FLAG not in out_flags:
            out_flags.append(CORRECTED_FLAG)
        if to_ist(new_out).time() >= parse_hhmm(values["attendance.check_out_earliest"]):
            out_flags = [f for f in out_flags if f != "EARLY"]
            resolved.append(("EARLY_CHECK_OUT", None))
    if row["location_check_in"]:
        in_flags = [f for f in in_flags if f != "OUT_OF_FENCE"] + [LOCATION_CORRECTED_FLAG]
        resolved.append(("OUT_OF_FENCE", "CHECK_IN"))
    if row["location_check_out"]:
        out_flags = [f for f in out_flags if f != "OUT_OF_FENCE"] + [LOCATION_CORRECTED_FLAG]
        resolved.append(("OUT_OF_FENCE", "CHECK_OUT"))
    if (new_in is not None or new_out is not None) and check_out_at is not None and not off_day:
        worked = check_out_at - check_in_at
        if half is not None:
            worked = hol.worked_in_halves(
                work_date, half.day_part, check_in_at, check_out_at, cfg.load_all(conn)
            )[1]
        if not short_day.is_short(conn, employee_id, work_date, worked, values):
            out_flags = [f for f in out_flags if f != short_day.KIND]
            resolved.append((short_day.KIND, None))
    conn.execute(
        text(
            "UPDATE hr.attendance_day SET check_in_at = :i, check_out_at = :o,"
            " check_in_flags = CAST(:fi AS jsonb), check_out_flags = CAST(:fo AS jsonb),"
            " updated_at = :n WHERE attendance_id = CAST(:a AS uuid)"
        ),
        {
            "i": check_in_at,
            "o": check_out_at,
            "fi": json.dumps(in_flags),
            "fo": json.dumps(out_flags),
            "n": now,
            "a": str(day["attendance_id"]),
        },
    )
    count = 0
    for kind, event in resolved:
        count += conn.execute(
            text(
                "UPDATE hr.attendance_exception SET status = 'RESOLVED', decided_at = :n,"
                " decided_by = :u, decision_note = 'Answered by an approved attendance correction'"
                " WHERE attendance_id = CAST(:a AS uuid) AND kind = :k"
                " AND (CAST(:ev AS text) IS NULL OR event = :ev)"
                " AND status IN ('PENDING', 'REJECTED')"
            ),
            {"n": now, "u": decided_by, "a": str(day["attendance_id"]), "k": kind, "ev": event},
        ).rowcount
    return {
        "previous_in": day["check_in_at"],
        "previous_out": day["check_out_at"],
        "resolved": count,
    }


def _closed_today(conn: Connection, employee_id: str, now: datetime) -> bool:
    """Past the time after which today is marked absent: 17:00, or the half-day split when this
    person's afternoon is a holiday today."""
    half = hol.half_day(ist_date(now), hol.holiday_for(conn, employee_id, ist_date(now)))
    return hol.closed_for_check_in(now, half.day_part if half else None, cfg.load_all(conn))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _view(r: Any) -> dict[str, Any]:
    return {
        "correctionId": str(r["correction_id"]),
        "workDate": r["work_date"].isoformat(),
        "requested": r["requested"],
        "reason": r["reason"],
        "status": r["status"],
        "checkInAt": _iso(r["check_in_at"]),
        "checkOutAt": _iso(r["check_out_at"]),
        "locationCheckIn": r["location_check_in"],
        "locationCheckOut": r["location_check_out"],
        # What the day had before an approved time correction was written to it.
        "previousCheckInAt": _iso(r["previous_check_in_at"]),
        "previousCheckOutAt": _iso(r["previous_check_out_at"]),
        "decisionNote": r["decision_note"],
        "decidedAt": _iso(r["decided_at"]),
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
    """An employee asks for a past working day to count as a full day or a half day, for a missed
    check-out to be recorded, or for a check-in or check-out time or location to be corrected."""
    employee_id = _own_employee_id(conn, user)
    now = clock()
    left_at = arrived_at = None
    if body.requested == ADJUST:
        arrived_at, left_at = _adjust_parts(conn, employee_id, body.work_date, body, now)
    elif body.check_in_time is not None or body.location_check_in or body.location_check_out:
        raise _invalid("A check-in time or a location is corrected with a time correction.")
    elif body.requested == CHECK_OUT:
        left_at = _check_out_moment(conn, employee_id, body.work_date, body.check_out_time, now)
    else:
        _check_day(
            conn,
            employee_id,
            body.work_date,
            ist_date(now),
            _closed_today(conn, employee_id, now),
        )
    # One waiting request per day; a day already approved as a full or half day is settled.
    live = conn.execute(
        text(
            "SELECT status FROM hr.attendance_correction WHERE employee_id = CAST(:e AS uuid)"
            " AND work_date = :d AND (status = 'PENDING' OR (status = 'APPROVED'"
            " AND requested IN ('FULL_DAY', 'HALF_DAY'))) FOR UPDATE"
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
                " check_out_at, check_in_at, location_check_in, location_check_out, created_at)"
                " VALUES (CAST(:e AS uuid), :d, :q, :r, :o, :i, :li, :lo, :n) RETURNING *"
            ),
            {
                "e": employee_id,
                "d": body.work_date,
                "q": body.requested,
                "r": body.reason,
                "o": left_at,
                "i": arrived_at,
                "li": body.requested == ADJUST and body.location_check_in,
                "lo": body.requested == ADJUST and body.location_check_out,
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
            "checkInAt": _iso(arrived_at),
            "checkOutAt": _iso(left_at),
            "locationCheckIn": bool(row["location_check_in"]),
            "locationCheckOut": bool(row["location_check_out"]),
        },
        request=request,
    )
    view = _view(row)
    stats = am.month_stats(conn, employee_id, body.work_date, now)
    view["approvalNote"] = stats.note()
    return view


@router.get("/attendance/corrections/me")
def my_corrections(
    user: HumanPrincipal = Depends(current_user),
    clock: Clock = Depends(get_clock),
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
    stats = am.StatsCache(conn, clock())
    items = []
    for r in rows:
        item = _view(r)
        # A waiting request says when only a Project Manager or Executive can approve it.
        item["approvalNote"] = (
            stats.get(employee_id, r["work_date"]).note() if r["status"] == "PENDING" else None
        )
        items.append(item)
    return {"items": items}


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
                "SELECT attendance_id, check_in_at, check_out_at, check_in_distance_m,"
                " check_out_distance_m, check_in_outlet_name, check_out_outlet_name"
                " FROM hr.attendance_day WHERE employee_id = CAST(:e AS uuid) AND work_date = :d"
            ),
            {"e": employee_id, "d": day},
        )
        .mappings()
        .first()
    )
    if record is None:
        return {"checkInAt": None, "checkOutAt": None, "exceptions": []}
    exceptions = [
        {"kind": r[0], "status": r[1], "event": r[2]}
        for r in conn.execute(
            text(
                "SELECT kind, status, event FROM hr.attendance_exception"
                " WHERE attendance_id = CAST(:a AS uuid) ORDER BY created_at"
            ),
            {"a": str(record["attendance_id"])},
        )
    ]

    def metres(value: Any) -> float | None:
        return float(value) if value is not None else None

    return {
        "checkInAt": _iso(record["check_in_at"]),
        "checkOutAt": _iso(record["check_out_at"]),
        "checkInDistanceM": metres(record["check_in_distance_m"]),
        "checkOutDistanceM": metres(record["check_out_distance_m"]),
        "checkInOutletName": record["check_in_outlet_name"],
        "checkOutOutletName": record["check_out_outlet_name"],
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
    """Correction requests this person may decide on. When the person's month has passed an
    attendance limit, only a Project Manager or Executive sees it (hrmgmt.attendance_month)."""
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
    stats = am.StatsCache(conn, now)
    decides: dict[tuple[str, bool], bool] = {}
    items = []
    today = ist_date(now)
    values = cfg.load_all(conn) if status == "PENDING" else {}
    for r in rows:
        eid = str(r["employee_id"])
        month = stats.get(eid, r["work_date"])
        key = (eid, month.escalated)
        if key not in decides:
            decides[key] = decides_attendance(
                conn, authorizer, user, eid, now, escalated=month.escalated
            )
        if not decides[key]:
            continue
        item = {
            **_view(r),
            "employeeId": eid,
            "employeeName": r["full_name"],
            "employeeCode": r["employee_code"],
            "day": _day_facts(conn, eid, r["work_date"]),
            "monthStats": month.view(),
            "approvalNote": month.note(),
        }
        if status == "PENDING":
            # The person's record with the request (product owner, 12 Nov 2026): leave taken in
            # the 4 weeks before the day asked about, and the average hours a week over 6 weeks.
            item["recentLeave"] = lr.recent_leave(conn, eid, r["work_date"], today)
            item["weeklyHours"] = weekly_hours(conn, eid, r["work_date"], now=now, values=values)
        items.append(item)
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
    background: BackgroundTasks,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    cid = _uuid(correction_id)
    row = (
        conn.execute(
            text(
                "SELECT employee_id, status, work_date, requested, check_out_at, check_in_at,"
                " location_check_in, location_check_out"
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
    escalated = am.month_stats(conn, employee_id, row["work_date"], now).escalated
    if not decides_attendance(
        conn, authorizer, user, employee_id, now, hr_override=True, escalated=escalated
    ):
        raise not_found("Request not found.")
    if row["status"] != "PENDING":
        raise conflict("APPROVAL_ALREADY_DECIDED", "This request has already been decided.")
    if body.decision == "REJECT" and not (body.note and body.note.strip()):
        raise ApiError(422, "APPROVAL_NOTE_REQUIRED", "Say why you are rejecting it.")
    status = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
    previous: dict[str, Any] = {"previous_in": None, "previous_out": None}
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
    if status == "APPROVED" and row["requested"] == ADJUST:
        previous = _apply_adjust(conn, row, employee_id, now, user.user_id)
    conn.execute(
        text(
            "UPDATE hr.attendance_correction SET status = :s, decided_by = :u, decided_at = :n,"
            " decision_note = :note, previous_check_in_at = :pi, previous_check_out_at = :po"
            " WHERE correction_id = CAST(:c AS uuid)"
        ),
        {
            "s": status,
            "u": user.user_id,
            "n": now,
            "note": (body.note or "").strip() or None,
            "pi": previous["previous_in"] if row["check_in_at"] is not None else None,
            "po": previous["previous_out"] if row["check_out_at"] is not None else None,
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
            "checkInAt": _iso(row["check_in_at"]),
            "checkOutAt": _iso(row["check_out_at"]),
            "locationCheckIn": row["location_check_in"],
            "locationCheckOut": row["location_check_out"],
            "previousCheckInAt": _iso(previous["previous_in"]),
            "previousCheckOutAt": _iso(previous["previous_out"]),
            "exceptionsResolved": previous.get("resolved", 0),
        },
        request=request,
    )
    if status == "APPROVED" and row["requested"] in (CHECK_OUT, ADJUST):
        notice = am.record_escalation(conn, employee_id, row["work_date"], now)
        if notice is not None:
            background.add_task(
                am.notify_employee,
                notice,
                provisioner=getattr(request.app.state, "provisioner", None),
                mailer=getattr(request.app.state, "mailer", None),
            )
    return {"correctionId": cid, "status": status}
