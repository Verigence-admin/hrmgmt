"""What HR sees of attendance for a day or a range: one row per employee, per day, per project.

Everything here is read from what is already stored (attendance days, exceptions, approved leave,
declared holidays and the copy of project assignments); nothing is written. A person on two
projects appears once for each, so the report can be read project by project."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import Connection, text

from hrmgmt import holidays as hol
from hrmgmt.attendance_correction import live_by_day
from hrmgmt.leavers import LAST_DAY_SQL, after_last_day, employed_on_or_after
from hrmgmt.timeutil import IST

MAX_RANGE_DAYS = 31

DELINQUENCY_LABELS = {
    "ABSENT": "Absent without approved leave",
    "MISSING_CHECK_OUT": "Checked in, never checked out",
    "LATE_CHECK_IN": "Late check-in",
    "EARLY_CHECK_OUT": "Early check-out",
    "OUT_OF_FENCE": "Not in tagged location",
    "NO_OUTLET_LOCATION": "No outlet location on file",
    "NO_FACE": "No face found in the photo",
    "FACE_MISMATCH": "Face does not match",
    "OFF_DAY_WORK": "Worked on an off day",
    "SHORT_DAY": "Fewer than the minimum hours",
    "CORRECTION_FULL_DAY": "Correction asked: full day",
    "CORRECTION_HALF_DAY": "Correction asked: half day",
}
# These three belong to one punch, so they say which: "No face found in the check-out photo".
_PER_PUNCH = {
    "OUT_OF_FENCE": "Not in tagged location at {side}",
    "NO_OUTLET_LOCATION": "No outlet location on file at {side}",
    "NO_FACE": "No face found in the {side} photo",
}
_SIDE_WORDS = {"in": "check-in", "out": "check-out"}


def delinquency_label(delinquency: dict[str, Any]) -> str:
    """The wording HR reads, on screen and in Excel. Names the punch when there is one."""
    code = delinquency["code"]
    side = delinquency.get("side")
    if code == "FACE_MISMATCH" and side in _SIDE_WORDS:
        # What the face was compared with: the profile photo, or the check-in photo of the day.
        if delinquency.get("ref") == "CHECK_IN":
            return "Face at check-out does not match the check-in photo"
        return f"Face does not match the profile photo at {_SIDE_WORDS[side]}"
    if side in _SIDE_WORDS and code in _PER_PUNCH:
        return _PER_PUNCH[code].format(side=_SIDE_WORDS[side])
    return DELINQUENCY_LABELS[code]


_NO_PROJECT: dict[str, Any] = {"projectCode": None, "projectName": None, "roles": [], "outlets": []}


@dataclass
class Row:
    work_date: date
    employee_id: str
    employee_code: str
    employee_name: str
    project_code: str | None
    project_name: str | None
    roles: list[str]
    outlets: list[str]
    status: str
    check_in_at: datetime | None = None
    check_out_at: datetime | None = None
    check_in_outlet: str | None = None
    check_out_outlet: str | None = None
    check_in_distance_m: float | None = None
    check_out_distance_m: float | None = None
    # True: outside the tagged location. False: inside it. None: not applicable (see _out_of_fence).
    check_in_out_of_fence: bool | None = None
    check_out_out_of_fence: bool | None = None
    # How closely the face matched (1 is identical) and what it was compared with. Shown to HR only.
    check_in_face_score: float | None = None
    check_out_face_score: float | None = None
    check_in_face_ref: str | None = None
    check_out_face_ref: str | None = None
    attendance_id: str | None = None
    has_check_in_photo: bool = False
    has_check_out_photo: bool = False
    delinquencies: list[dict[str, Any]] = field(default_factory=list)
    correction: dict[str, Any] | None = None
    # The person's own day: their holidays may be for some projects or outlets only, or half a day.
    day_kind: str = "WORKING"
    holiday: str | None = None
    half_day_holiday: dict[str, Any] | None = None
    working_sunday: dict[str, Any] | None = None

    @property
    def hours(self) -> float | None:
        if self.check_in_at and self.check_out_at:
            return round((self.check_out_at - self.check_in_at).total_seconds() / 3600, 1)
        return None


def _out_of_fence(record: Any, side: str) -> bool | None:
    """Whether this check-in or check-out was outside the tagged location, read from what was stored at
    the time. Only for someone the fence applies to and only when their outlet had a location on file;
    for everyone else there is nothing to say, so None (never False)."""
    if not record["geofenced"] or record[f"check_{side}_at"] is None:
        return None
    flags = record[f"check_{side}_flags"] or []
    if record[f"check_{side}_outlet_id"] is None or "NO_OUTLET_LOCATION" in flags:
        return None
    return "OUT_OF_FENCE" in flags


def _bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=IST)
    return start, start + timedelta(days=1)


def build_rows(
    conn: Connection,
    first: date,
    last: date,
    *,
    today: date,
    project_code: str | None = None,
    employee_id: str | None = None,
    closed_today: bool = False,
    now: datetime | None = None,
    values: dict[str, Any] | None = None,
) -> list[Row]:
    """closed_today: it is past attendance.absent_after (17:00), so today's missing check-ins are
    absences marked by the system, not "not checked in yet". With `now` and `values` given, a person
    whose afternoon is a holiday today is closed from the half-day split (14:00) instead."""
    window_start, _ = _bounds(first)
    _, window_end = _bounds(last)
    params: dict[str, Any] = {"a": first, "b": last, "e": employee_id}

    employees = (
        conn.execute(
            text(
                f"""
                SELECT e.employee_id, e.employee_code, e.full_name, e.security_user_id,
                       e.date_of_joining, {LAST_DAY_SQL} AS last_day
                FROM hr.employee e
                WHERE {employed_on_or_after("a")}
                  AND (CAST(:e AS uuid) IS NULL OR e.employee_id = CAST(:e AS uuid))
                ORDER BY lower(e.full_name), e.employee_code
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    days = {
        (str(r["employee_id"]), r["work_date"]): r
        for r in conn.execute(
            text("SELECT * FROM hr.attendance_day WHERE work_date >= :a AND work_date <= :b"),
            params,
        ).mappings()
    }
    exceptions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in conn.execute(
        text(
            "SELECT attendance_id, kind, status, reason, event FROM hr.attendance_exception"
            " WHERE work_date >= :a AND work_date <= :b ORDER BY created_at"
        ),
        params,
    ).mappings():
        exceptions[str(r["attendance_id"])].append(
            {
                "kind": r["kind"],
                "status": r["status"],
                "reason": r["reason"],
                "side": "in" if r["event"] == "CHECK_IN" else "out",
            }
        )
    leave = [
        (str(r["employee_id"]), r["from_date"], r["to_date"])
        for r in conn.execute(
            text(
                "SELECT employee_id, from_date, to_date FROM hr.leave_request"
                " WHERE status = 'APPROVED' AND from_date <= :b AND to_date >= :a"
            ),
            params,
        ).mappings()
    ]
    # Leave the Team Lead or Project Manager has not decided yet (product owner, 6 Nov 2026).
    applied = [
        (str(r["employee_id"]), r["from_date"], r["to_date"])
        for r in conn.execute(
            text(
                "SELECT employee_id, from_date, to_date FROM hr.leave_request"
                " WHERE status = 'PENDING' AND NOT half_day AND from_date <= :b AND to_date >= :a"
            ),
            params,
        ).mappings()
    ]
    corrections = live_by_day(conn, first, last)
    by_person = hol.holidays_by_employee(
        conn, [str(e["employee_id"]) for e in employees], first, last
    )
    assignments: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in conn.execute(
        text(
            "SELECT security_user_id, project_code, project_name, role_code, outlet_name,"
            " valid_from, valid_to FROM hr.work_assignment"
            " WHERE valid_from < :end AND (valid_to IS NULL OR valid_to > :start)"
            " ORDER BY project_name, role_code, outlet_name"
        ),
        {"start": window_start, "end": window_end},
    ).mappings():
        assignments[str(r["security_user_id"])].append(dict(r))

    rows: list[Row] = []
    day = first
    while day <= last:
        start, end = _bounds(day)
        for emp in employees:
            eid = str(emp["employee_id"])
            holiday = by_person.get(eid, {}).get(day)
            non_working = hol.is_off_day(day, holiday)
            half = hol.half_day(day, holiday)
            joined = emp["date_of_joining"]
            record = days.get((eid, day))
            # Before the joining date there is nothing to expect, but a check-in that was made is
            # always shown, so a wrong joining date can never hide someone who was at work.
            if joined is not None and joined > day and record is None:
                continue
            # Someone who has left is shown up to their last day (2026-11-09).
            if after_last_day(emp["last_day"], day) and record is None:
                continue
            if non_working and record is None:
                continue
            on_leave = any(e == eid and f <= day <= t for e, f, t in leave)
            leave_applied = any(e == eid and f <= day <= t for e, f, t in applied)
            closed_now = closed_today
            if day == today and now is not None and values is not None:
                closed_now = hol.closed_for_check_in(now, half.day_part if half else None, values)
            closed = day < today or (day == today and closed_now)
            projects = _projects_on(assignments.get(str(emp["security_user_id"]), []), start, end)
            for project in projects:
                if project_code and project["projectCode"] != project_code:
                    continue
                row = _row(
                    emp,
                    eid,
                    day,
                    record,
                    exceptions,
                    on_leave,
                    project,
                    today=today,
                    closed=closed,
                    leave_applied=leave_applied,
                )
                row.day_kind, row.holiday = hol.day_kind(day, holiday)
                row.working_sunday = hol.working_sunday_view(day, holiday)
                if half is not None and values is not None:
                    row.half_day_holiday = hol.half_day_view(half, values)
                elif half is not None:
                    row.half_day_holiday = {"name": half.name, "dayPart": half.day_part}
                _apply_correction(row, corrections.get((eid, day)))
                rows.append(row)
        day += timedelta(days=1)
    return rows


def _projects_on(
    assignments: list[dict[str, Any]], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for a in assignments:
        if a["valid_from"] >= end or (a["valid_to"] is not None and a["valid_to"] <= start):
            continue
        key = (a["project_code"], a["project_name"])
        entry = grouped.setdefault(
            key,
            {"projectCode": key[0], "projectName": key[1], "roles": [], "outlets": []},
        )
        if a["role_code"] not in entry["roles"]:
            entry["roles"].append(a["role_code"])
        if a["outlet_name"] and a["outlet_name"] not in entry["outlets"]:
            entry["outlets"].append(a["outlet_name"])
    return list(grouped.values()) or [dict(_NO_PROJECT)]


def _row(
    emp: Any,
    eid: str,
    day: date,
    record: Any,
    exceptions: dict[str, list[dict[str, Any]]],
    on_leave: bool,
    project: dict[str, Any],
    *,
    today: date,
    closed: bool = False,
    leave_applied: bool = False,
) -> Row:
    row = Row(
        work_date=day,
        employee_id=eid,
        employee_code=emp["employee_code"],
        employee_name=emp["full_name"],
        project_code=project["projectCode"],
        project_name=project["projectName"],
        roles=list(project["roles"]),
        outlets=list(project["outlets"]),
        status="",
    )
    past = day < today
    if record is None:
        if on_leave:
            row.status = "ON_LEAVE"
        elif leave_applied:
            # Counted as absent in payroll until it is approved; shown for what it is.
            row.status = "LEAVE_APPLIED"
        elif past or closed:
            row.status = "ABSENT"
            row.delinquencies.append({"code": "ABSENT", "status": None})
        else:
            row.status = "NOT_CHECKED_IN"
        return row
    row.attendance_id = str(record["attendance_id"])
    row.has_check_in_photo = bool(record["check_in_photo_key"])
    row.has_check_out_photo = bool(record["check_out_photo_key"])
    row.check_in_at = record["check_in_at"]
    row.check_out_at = record["check_out_at"]
    row.check_in_outlet = record["check_in_outlet_name"]
    row.check_out_outlet = record["check_out_outlet_name"]
    for side in ("in", "out"):
        value = record[f"check_{side}_distance_m"]
        setattr(row, f"check_{side}_distance_m", float(value) if value is not None else None)
        setattr(row, f"check_{side}_out_of_fence", _out_of_fence(record, side))
        score = record[f"check_{side}_face_score"]
        setattr(row, f"check_{side}_face_score", float(score) if score is not None else None)
        setattr(row, f"check_{side}_face_ref", record[f"check_{side}_face_ref"])
    found = exceptions.get(str(record["attendance_id"]), [])
    row.delinquencies.extend(
        {"code": e["kind"], "status": e["status"], "reason": e["reason"], "side": e["side"]}
        for e in found
    )
    for side in ("in", "out"):
        flags = record[f"check_{side}_flags"] or []
        for code in ("NO_OUTLET_LOCATION", "NO_FACE", "FACE_MISMATCH"):
            # Not for anyone to approve: HR fixes the outlet, or looks at the photo.
            if code in flags and not any(e["kind"] == code and e["side"] == side for e in found):
                row.delinquencies.append(
                    {
                        "code": code,
                        "status": None,
                        "reason": None,
                        "side": side,
                        "ref": record[f"check_{side}_face_ref"],
                    }
                )
    if record["check_out_at"] is None and past:
        row.delinquencies.append({"code": "MISSING_CHECK_OUT", "status": None})
    if any(e["status"] == "PENDING" for e in found):
        row.status = "PENDING_APPROVAL"
    elif any(e["status"] == "REJECTED" for e in found):
        row.status = "EXCEPTION_REJECTED"
    elif record["check_out_at"] is None:
        row.status = "MISSING_CHECK_OUT" if past else "CHECKED_IN"
    else:
        row.status = "COMPLETE"
    return row


def _apply_correction(row: Row, correction: dict[str, Any] | None) -> None:
    """A pending correction waits like an exception; an approved one is what the day counts, so the
    day is no longer absent. What happened on the day stays listed alongside it."""
    if correction is None:
        return
    row.correction = correction
    row.delinquencies.append(
        {
            "code": f"CORRECTION_{correction['requested']}",
            "status": correction["status"],
            "reason": correction["reason"],
            "side": None,
        }
    )
    if correction["status"] == "APPROVED":
        row.delinquencies = [d for d in row.delinquencies if d["code"] != "ABSENT"]
        row.status = "CORRECTED"
    elif row.status in ("ABSENT", "MISSING_CHECK_OUT", "EXCEPTION_REJECTED"):
        row.status = "PENDING_APPROVAL"


def row_view(row: Row) -> dict[str, Any]:
    return {
        "workDate": row.work_date.isoformat(),
        "employeeId": row.employee_id,
        "employeeCode": row.employee_code,
        "employeeName": row.employee_name,
        "projectCode": row.project_code,
        "projectName": row.project_name,
        "roles": row.roles,
        "outlets": row.outlets,
        "status": row.status,
        "checkInAt": row.check_in_at.isoformat() if row.check_in_at else None,
        "checkOutAt": row.check_out_at.isoformat() if row.check_out_at else None,
        "checkInOutlet": row.check_in_outlet,
        "checkOutOutlet": row.check_out_outlet,
        "checkInDistanceM": row.check_in_distance_m,
        "checkOutDistanceM": row.check_out_distance_m,
        "checkInFaceScore": row.check_in_face_score,
        "checkInFaceRef": row.check_in_face_ref,
        "checkOutFaceScore": row.check_out_face_score,
        "checkOutFaceRef": row.check_out_face_ref,
        "checkInOutOfFence": row.check_in_out_of_fence,
        "checkOutOutOfFence": row.check_out_out_of_fence,
        "hoursWorked": row.hours,
        "attendanceId": row.attendance_id,
        "hasCheckInPhoto": row.has_check_in_photo,
        "hasCheckOutPhoto": row.has_check_out_photo,
        "correction": row.correction,
        "dayKind": row.day_kind,
        "holiday": row.holiday,
        "halfDayHoliday": row.half_day_holiday,
        "workingSunday": row.working_sunday,
        "delinquencies": [
            {
                "code": d["code"],
                "label": delinquency_label(d),
                "side": {"in": "CHECK_IN", "out": "CHECK_OUT"}.get(d.get("side") or ""),
                "decision": d["status"],
                "reason": d.get("reason"),
            }
            for d in row.delinquencies
        ],
    }
