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
) -> list[Row]:
    window_start, _ = _bounds(first)
    _, window_end = _bounds(last)
    params: dict[str, Any] = {"a": first, "b": last, "e": employee_id}

    employees = (
        conn.execute(
            text(
                """
                SELECT employee_id, employee_code, full_name, security_user_id, date_of_joining
                FROM hr.employee
                WHERE employment_status = 'ACTIVE'
                  AND (CAST(:e AS uuid) IS NULL OR employee_id = CAST(:e AS uuid))
                ORDER BY lower(full_name), employee_code
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
    holidays = {
        r[0]
        for r in conn.execute(
            text(
                "SELECT holiday_date FROM hr.holiday"
                " WHERE status = 'DECLARED' AND holiday_date >= :a AND holiday_date <= :b"
            ),
            params,
        )
    }
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
        non_working = day.weekday() == 6 or day in holidays
        for emp in employees:
            eid = str(emp["employee_id"])
            joined = emp["date_of_joining"]
            if joined is not None and joined > day:
                continue
            record = days.get((eid, day))
            if non_working and record is None:
                continue
            on_leave = any(e == eid and f <= day <= t for e, f, t in leave)
            projects = _projects_on(assignments.get(str(emp["security_user_id"]), []), start, end)
            for project in projects:
                if project_code and project["projectCode"] != project_code:
                    continue
                rows.append(_row(emp, eid, day, record, exceptions, on_leave, project, today=today))
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
        elif past:
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
