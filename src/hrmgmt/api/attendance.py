from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from hrmgmt import permissions as perm
from hrmgmt import settings_store as cfg
from hrmgmt import workcontext as wc
from hrmgmt.api.employees import _own_employee_id
from hrmgmt.approvals import decides_attendance
from hrmgmt.audit import record_audit
from hrmgmt.authz import Authorizer
from hrmgmt.db import get_conn
from hrmgmt.errors import ApiError, conflict, dependency_unavailable, not_found
from hrmgmt.facecheck import face_present
from hrmgmt.geo import haversine_m
from hrmgmt.geocode import ReverseGeocoder
from hrmgmt.principal import current_user, get_authorizer, has_permission, require_permission
from hrmgmt.security import HumanPrincipal
from hrmgmt.stamp import StampError, exif_capture_time, read_photo, stamp_photo
from hrmgmt.storage import ObjectStorage, StorageError
from hrmgmt.timeutil import Clock, is_sunday, ist_date, parse_hhmm, to_ist, utc_now

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/hr/v1", tags=["Attendance"])

can_read_all = require_permission(perm.HR_ATTENDANCE_READ_ALL)

Event = Literal["CHECK_IN", "CHECK_OUT"]
_PHOTO_TIME_WINDOW = timedelta(minutes=5)


def get_clock(request: Request) -> Clock:
    return getattr(request.app.state, "clock", None) or utc_now


def get_storage(request: Request) -> ObjectStorage | None:
    return getattr(request.app.state, "storage", None)


def get_geocoder(request: Request) -> ReverseGeocoder | None:
    return getattr(request.app.state, "geocoder", None)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---- working days --------------------------------------------------------------------------


def day_kind(conn: Connection, day: date) -> tuple[str, str | None]:
    """('WORKING', None), ('SUNDAY', None) or ('HOLIDAY', name). Only a declared holiday stops work."""
    if is_sunday(day):
        return "SUNDAY", None
    row = conn.execute(
        text("SELECT name FROM hr.holiday WHERE holiday_date = :d AND status = 'DECLARED'"),
        {"d": day},
    ).first()
    if row:
        return "HOLIDAY", str(row[0])
    return "WORKING", None


def tentative_holiday(conn: Connection, day: date) -> str | None:
    row = conn.execute(
        text("SELECT name FROM hr.holiday WHERE holiday_date = :d AND status = 'TENTATIVE'"),
        {"d": day},
    ).first()
    return str(row[0]) if row else None


# ---- capture token -------------------------------------------------------------------------


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: Event


@router.post("/attendance/capture-token")
def capture_token(
    body: TokenRequest,
    user: HumanPrincipal = Depends(current_user),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """One-time permission to take one photo, valid for a couple of minutes. A photo without a
    valid, unused token is refused."""
    employee_id = _own_employee_id(conn, user)
    ttl = int(cfg.get(conn, "attendance.capture_token_ttl_s"))
    now = clock()
    token = secrets.token_urlsafe(32)
    conn.execute(
        text(
            "INSERT INTO hr.capture_token (token_hash, employee_id, purpose, created_at, expires_at)"
            " VALUES (:h, CAST(:e AS uuid), :p, :n, :x)"
        ),
        {
            "h": _hash(token),
            "e": employee_id,
            "p": body.purpose,
            "n": now,
            "x": now + timedelta(seconds=ttl),
        },
    )
    return {
        "token": token,
        "ttlSeconds": ttl,
        "expiresAt": (now + timedelta(seconds=ttl)).isoformat(),
    }


# ---- check in and out ----------------------------------------------------------------------


def _exceptions_needed(
    conn: Connection,
    *,
    event: Event,
    user_id: str,
    now: datetime,
    lat: float,
    lon: float,
    reason: str | None,
    employee_roles: set[str],
    assignments: list[dict[str, Any]],
) -> tuple[bool, float | None, dict[str, Any] | None, list[str], list[str]]:
    """Returns (fenced, distance, outlet, exception_kinds, flags). Pure decision logic."""
    values = cfg.load_all(conn)
    ist = to_ist(now)
    kinds: list[str] = []
    flags: list[str] = []
    fenced = "PC" in employee_roles and not (employee_roles & {"TL", "PM"})
    distance: float | None = None
    outlet: dict[str, Any] | None = None
    if fenced:
        outlets = [a for a in assignments if a["role_code"] == "PC" and a["outlet_id"] is not None]
        with_position = [
            o for o in outlets if o["latitude"] is not None and o["longitude"] is not None
        ]
        if not with_position:
            # Missing outlet data is a system gap, not the person's fault: no reason, no approval.
            # The flag on the record is what HR sees in the daily view.
            flags.append("NO_OUTLET_LOCATION")
        else:
            nearest = min(
                with_position,
                key=lambda o: haversine_m(lat, lon, float(o["latitude"]), float(o["longitude"])),
            )
            distance = haversine_m(
                lat, lon, float(nearest["latitude"]), float(nearest["longitude"])
            )
            outlet = nearest
            if distance > float(values["attendance.geofence_radius_m"]):
                kinds.append("OUT_OF_FENCE")
                flags.append("OUT_OF_FENCE")
    if event == "CHECK_IN" and ist.time() > parse_hhmm(values["attendance.late_after"]):
        kinds.append("LATE_CHECK_IN")
        flags.append("LATE")
    if event == "CHECK_OUT" and ist.time() < parse_hhmm(values["attendance.check_out_earliest"]):
        kinds.append("EARLY_CHECK_OUT")
        flags.append("EARLY")
    return fenced, distance, outlet, kinds, flags


def _record_event(
    event: Event,
    request: Request,
    *,
    photo: UploadFile,
    token: str,
    latitude: float,
    longitude: float,
    accuracy_m: float,
    position_age_s: float,
    reason: str | None,
    user: HumanPrincipal,
    clock: Clock,
    storage: ObjectStorage | None,
    geocoder: ReverseGeocoder | None,
    conn: Connection,
    data: bytes,
) -> dict[str, Any]:
    if storage is None:
        raise dependency_unavailable("Photo storage is not configured.")
    started = time.perf_counter()
    employee_id = _own_employee_id(conn, user)
    now = clock()
    work_date = ist_date(now)
    values = cfg.load_all(conn)

    # A Sunday or a declared holiday is a day off, but attendance can still be marked: it goes for
    # approval (below) instead of being refused.
    off_day = day_kind(conn, work_date)[0] in ("SUNDAY", "HOLIDAY")

    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ApiError(422, "ATTENDANCE_LOCATION_INVALID", "The location is not valid.")
    if accuracy_m < 0 or accuracy_m > float(values["attendance.max_accuracy_m"]):
        raise ApiError(
            422,
            "ATTENDANCE_LOCATION_TOO_INACCURATE",
            f"Your location is not accurate enough ({accuracy_m:.0f} m). Move to an open area and try again.",
        )
    if position_age_s < 0 or position_age_s > float(values["attendance.max_position_age_s"]):
        raise ApiError(
            422,
            "ATTENDANCE_LOCATION_TOO_OLD",
            "Your location is out of date. Wait for a fresh location and try again.",
        )

    day = (
        conn.execute(
            text(
                "SELECT attendance_id, check_in_at, check_out_at FROM hr.attendance_day"
                " WHERE employee_id = CAST(:e AS uuid) AND work_date = :d FOR UPDATE"
            ),
            {"e": employee_id, "d": work_date},
        )
        .mappings()
        .first()
    )
    if event == "CHECK_IN" and day is not None and day["check_in_at"] is not None:
        raise conflict("ATTENDANCE_ALREADY_CHECKED_IN", "You have already checked in today.")
    if event == "CHECK_OUT":
        if day is None or day["check_in_at"] is None:
            raise conflict("ATTENDANCE_NOT_CHECKED_IN", "Check in first.")
        if day["check_out_at"] is not None:
            raise conflict("ATTENDANCE_ALREADY_CHECKED_OUT", "You have already checked out today.")

    try:
        image = read_photo(data)
    except StampError as exc:
        raise ApiError(422, "ATTENDANCE_PHOTO_NOT_ACCEPTED", str(exc)) from exc

    user_id = wc.employee_user_id(conn, employee_id)
    assignments = wc.assignments_at(conn, user_id, now) if user_id else []
    roles = {a["role_code"] for a in assignments}
    fenced, distance, outlet, kinds, flags = _exceptions_needed(
        conn,
        event=event,
        user_id=user_id or "",
        now=now,
        lat=latitude,
        lon=longitude,
        reason=reason,
        employee_roles=roles,
        assignments=assignments,
    )
    if off_day:
        # Being late or early means nothing on a day off; working on it needs approval instead.
        kinds[:] = [k for k in kinds if k not in ("LATE_CHECK_IN", "EARLY_CHECK_OUT")]
        flags[:] = [f for f in flags if f not in ("LATE", "EARLY")]
        if event == "CHECK_IN":
            kinds.append("OFF_DAY_WORK")
            flags.append("OFF_DAY")
    if "OUT_OF_FENCE" in kinds and not (reason and reason.strip()):
        # The token is not used up: the person adds a reason and sends the same photo again.
        raise ApiError(
            422,
            "ATTENDANCE_REASON_REQUIRED",
            "You are not at your tagged location. Say why, and your Team Lead or Project Manager will review it.",
        )

    # The token: one use, for this person and this event, still within its window.
    used = conn.execute(
        text(
            "UPDATE hr.capture_token SET used_at = :n"
            " WHERE token_hash = :h AND employee_id = CAST(:e AS uuid) AND purpose = :p"
            " AND used_at IS NULL AND expires_at > :n RETURNING 1"
        ),
        {"n": now, "h": _hash(token), "e": employee_id, "p": event},
    ).first()
    if used is None:
        raise conflict(
            "ATTENDANCE_TOKEN_INVALID", "This photo session has expired. Take the photo again."
        )

    taken = exif_capture_time(data)
    if taken is not None and abs(taken - to_ist(now).replace(tzinfo=None)) > _PHOTO_TIME_WINDOW:
        flags.append("PHOTO_TIME_MISMATCH")
    # Only whether a face is in the picture; nobody is identified. A photo is never refused for it:
    # it is flagged, and HR sees the flag.
    checked = time.perf_counter()
    if face_present(image) is False:
        flags.append("NO_FACE")
    face_checked = time.perf_counter()

    address = geocoder.address(latitude, longitude) if geocoder else None
    geocoded = time.perf_counter()
    if address is None:
        flags.append("NO_ADDRESS")
    name_row = conn.execute(
        text(
            "SELECT full_name, employee_code FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"
        ),
        {"e": employee_id},
    ).first()
    stamped = stamp_photo(
        image,
        when=now,
        name=f"{name_row[0]} ({name_row[1]})",
        latitude=latitude,
        longitude=longitude,
        address=address,
        event="Check-in" if event == "CHECK_IN" else "Check-out",
    )
    stamped_at = time.perf_counter()
    key = (
        f"attendance/{work_date:%Y}/{work_date:%m}/{employee_id}/"
        f"{work_date.isoformat()}-{'in' if event == 'CHECK_IN' else 'out'}.jpg"
    )
    try:
        storage.put(key, stamped, "image/jpeg")
    except StorageError as exc:
        raise dependency_unavailable("The photo could not be saved. Please try again.") from exc
    stored = time.perf_counter()

    prefix = "check_in" if event == "CHECK_IN" else "check_out"
    params = {
        "e": employee_id,
        "d": work_date,
        "at": now,
        "lat": latitude,
        "lon": longitude,
        "acc": accuracy_m,
        "dist": round(distance, 1) if distance is not None else None,
        "oid": outlet["outlet_id"] if outlet else None,
        "oname": outlet["outlet_name"] if outlet else None,
        "addr": address,
        "key": key,
        "flags": json.dumps(flags),
        "fenced": fenced,
    }
    if day is None:
        attendance_id = str(
            conn.execute(
                text(
                    f"""
                    INSERT INTO hr.attendance_day
                        (employee_id, work_date, geofenced, {prefix}_at, {prefix}_lat, {prefix}_lon,
                         {prefix}_accuracy_m, {prefix}_distance_m, {prefix}_outlet_id,
                         {prefix}_outlet_name, {prefix}_address, {prefix}_photo_key, {prefix}_flags)
                    VALUES (CAST(:e AS uuid), :d, :fenced, :at, :lat, :lon, :acc, :dist,
                            CAST(:oid AS uuid), :oname, :addr, :key, CAST(:flags AS jsonb))
                    RETURNING attendance_id
                    """
                ),
                params,
            ).scalar_one()
        )
    else:
        attendance_id = str(day["attendance_id"])
        conn.execute(
            text(
                f"""
                UPDATE hr.attendance_day SET geofenced = geofenced OR :fenced,
                    {prefix}_at = :at, {prefix}_lat = :lat, {prefix}_lon = :lon,
                    {prefix}_accuracy_m = :acc, {prefix}_distance_m = :dist,
                    {prefix}_outlet_id = CAST(:oid AS uuid), {prefix}_outlet_name = :oname,
                    {prefix}_address = :addr, {prefix}_photo_key = :key,
                    {prefix}_flags = CAST(:flags AS jsonb), updated_at = now()
                WHERE attendance_id = CAST(:id AS uuid)
                """
            ),
            {**params, "id": attendance_id},
        )
    for exception_kind in kinds:
        conn.execute(
            text(
                "INSERT INTO hr.attendance_exception"
                " (attendance_id, employee_id, work_date, event, kind, reason)"
                " VALUES (CAST(:a AS uuid), CAST(:e AS uuid), :d, :ev, :k, :r)"
            ),
            {
                "a": attendance_id,
                "e": employee_id,
                "d": work_date,
                "ev": event,
                "k": exception_kind,
                "r": (reason or "").strip() or None,
            },
        )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="ATTENDANCE_CHECK_IN" if event == "CHECK_IN" else "ATTENDANCE_CHECK_OUT",
        entity_type="employee",
        entity_id=employee_id,
        changes={
            "workDate": work_date.isoformat(),
            "flags": flags,
            "exceptions": kinds,
            "distanceM": round(distance, 1) if distance is not None else None,
        },
        request=request,
    )
    # Where the time goes on a check-in or check-out: the checks, the address lookup, drawing on the
    # photo, and storing it. Milliseconds only; nothing about the person.
    logger.info(
        "hr_attendance_timing",
        side=event,
        photo_kb=len(data) // 1024,
        checks_ms=round((checked - started) * 1000),
        face_ms=round((face_checked - checked) * 1000),
        address_ms=round((geocoded - face_checked) * 1000),
        stamp_ms=round((stamped_at - geocoded) * 1000),
        store_ms=round((stored - stamped_at) * 1000),
        total_ms=round((time.perf_counter() - started) * 1000),
    )
    return {
        "attendanceId": attendance_id,
        "workDate": work_date.isoformat(),
        "event": event,
        "at": now.isoformat(),
        "distanceM": round(distance, 1) if distance is not None else None,
        "outletName": outlet["outlet_name"] if outlet else None,
        "address": address,
        "flags": flags,
        "needsApproval": kinds,
    }


async def _read(file: UploadFile) -> bytes:
    from hrmgmt.stamp import MAX_UPLOAD_BYTES

    return await file.read(MAX_UPLOAD_BYTES + 1)


@router.post("/attendance/check-in")
async def check_in(
    request: Request,
    photo: UploadFile = File(...),
    token: str = Form(..., max_length=100),
    latitude: float = Form(...),
    longitude: float = Form(...),
    accuracy_m: float = Form(...),
    position_age_s: float = Form(0),
    reason: str | None = Form(None, max_length=500),
    user: HumanPrincipal = Depends(current_user),
    clock: Clock = Depends(get_clock),
    storage: ObjectStorage | None = Depends(get_storage),
    geocoder: ReverseGeocoder | None = Depends(get_geocoder),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    return _record_event(
        "CHECK_IN",
        request,
        photo=photo,
        token=token,
        latitude=latitude,
        longitude=longitude,
        accuracy_m=accuracy_m,
        position_age_s=position_age_s,
        reason=reason,
        user=user,
        clock=clock,
        storage=storage,
        geocoder=geocoder,
        conn=conn,
        data=await _read(photo),
    )


@router.post("/attendance/check-out")
async def check_out(
    request: Request,
    photo: UploadFile = File(...),
    token: str = Form(..., max_length=100),
    latitude: float = Form(...),
    longitude: float = Form(...),
    accuracy_m: float = Form(...),
    position_age_s: float = Form(0),
    reason: str | None = Form(None, max_length=500),
    user: HumanPrincipal = Depends(current_user),
    clock: Clock = Depends(get_clock),
    storage: ObjectStorage | None = Depends(get_storage),
    geocoder: ReverseGeocoder | None = Depends(get_geocoder),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    return _record_event(
        "CHECK_OUT",
        request,
        photo=photo,
        token=token,
        latitude=latitude,
        longitude=longitude,
        accuracy_m=accuracy_m,
        position_age_s=position_age_s,
        reason=reason,
        user=user,
        clock=clock,
        storage=storage,
        geocoder=geocoder,
        conn=conn,
        data=await _read(photo),
    )


# ---- reading ------------------------------------------------------------------------------


def _day_view(row: Any, exceptions: list[dict[str, Any]]) -> dict[str, Any]:
    def side(prefix: str) -> dict[str, Any] | None:
        at = row[f"{prefix}_at"]
        if at is None:
            return None
        return {
            "at": at.isoformat(),
            "distanceM": float(row[f"{prefix}_distance_m"])
            if row[f"{prefix}_distance_m"] is not None
            else None,
            "outletName": row[f"{prefix}_outlet_name"],
            "address": row[f"{prefix}_address"],
            "flags": row[f"{prefix}_flags"],
            "hasPhoto": bool(row[f"{prefix}_photo_key"]),
        }

    pending = [e for e in exceptions if e["status"] == "PENDING"]
    rejected = [e for e in exceptions if e["status"] == "REJECTED"]
    if row["check_in_at"] is None:
        status = "ABSENT"
    elif pending:
        status = "PENDING_APPROVAL"
    elif rejected:
        status = "EXCEPTION_REJECTED"
    elif row["check_out_at"] is None:
        status = "CHECKED_IN"
    else:
        status = "COMPLETE"
    return {
        "attendanceId": str(row["attendance_id"]),
        "workDate": row["work_date"].isoformat(),
        "status": status,
        "geofenced": row["geofenced"],
        "checkIn": side("check_in"),
        "checkOut": side("check_out"),
        "exceptions": exceptions,
    }


def _exceptions_for(conn: Connection, attendance_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {a: [] for a in attendance_ids}
    if not attendance_ids:
        return out
    rows = conn.execute(
        text(
            "SELECT exception_id, attendance_id, event, kind, reason, status, decision_note, decided_at"
            " FROM hr.attendance_exception WHERE attendance_id = ANY(CAST(:ids AS uuid[]))"
            " ORDER BY created_at"
        ),
        {"ids": attendance_ids},
    ).mappings()
    for r in rows:
        out[str(r["attendance_id"])].append(
            {
                "exceptionId": str(r["exception_id"]),
                "event": r["event"],
                "kind": r["kind"],
                "reason": r["reason"],
                "status": r["status"],
                "decisionNote": r["decision_note"],
                "decidedAt": r["decided_at"].isoformat() if r["decided_at"] else None,
            }
        )
    return out


@router.get("/attendance/today")
def today(
    user: HumanPrincipal = Depends(current_user),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    now = clock()
    work_date = ist_date(now)
    kind, holiday = day_kind(conn, work_date)
    values = cfg.load_all(conn)
    row = (
        conn.execute(
            text(
                "SELECT * FROM hr.attendance_day WHERE employee_id = CAST(:e AS uuid) AND work_date = :d"
            ),
            {"e": employee_id, "d": work_date},
        )
        .mappings()
        .first()
    )
    day = None
    if row is not None:
        day = _day_view(
            row, _exceptions_for(conn, [str(row["attendance_id"])])[str(row["attendance_id"])]
        )
    user_id = wc.employee_user_id(conn, employee_id)
    assignments = wc.assignments_at(conn, user_id, now) if user_id else []
    roles = sorted({a["role_code"] for a in assignments})
    fenced = "PC" in roles and not ({"TL", "PM"} & set(roles))
    outlets = [
        {
            "outletName": a["outlet_name"],
            "projectName": a["project_name"],
            "hasLocation": a["latitude"] is not None and a["longitude"] is not None,
        }
        for a in assignments
        if a["role_code"] == "PC" and a["outlet_id"] is not None
    ]
    sync = wc.sync_status(conn)
    return {
        "workDate": work_date.isoformat(),
        "serverTime": to_ist(now).isoformat(),
        "dayKind": kind,
        "holidayName": holiday,
        "tentativeHoliday": tentative_holiday(conn, work_date),
        "day": day,
        "roles": roles,
        "geofenced": fenced,
        "geofenceRadiusM": int(values["attendance.geofence_radius_m"]),
        "outlets": outlets if fenced else [],
        "workContextAgeHours": (
            round((now - sync["last_success_at"]).total_seconds() / 3600, 1)
            if sync.get("last_success_at")
            else None
        ),
        "standardTimes": {
            "checkIn": values["attendance.check_in_standard"],
            "lateAfter": values["attendance.late_after"],
            "checkOutEarliest": values["attendance.check_out_earliest"],
            "checkOut": values["attendance.check_out_standard"],
        },
    }


def _month_bounds(month: str | None, today_date: date) -> tuple[date, date]:
    if month:
        try:
            year, mon = (int(x) for x in month.split("-"))
            first = date(year, mon, 1)
        except ValueError as exc:
            raise ApiError(422, "HR_VALIDATION_FAILED", "Month must look like 2026-10.") from exc
    else:
        first = today_date.replace(day=1)
    nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return first, nxt


@router.get("/attendance/me")
def my_attendance(
    month: Annotated[str | None, Query(max_length=7)] = None,
    user: HumanPrincipal = Depends(current_user),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    first, nxt = _month_bounds(month, ist_date(clock()))
    return {"month": first.strftime("%Y-%m"), "days": _days(conn, employee_id, first, nxt)}


def _days(conn: Connection, employee_id: str, first: date, nxt: date) -> list[dict[str, Any]]:
    rows = (
        conn.execute(
            text(
                "SELECT * FROM hr.attendance_day WHERE employee_id = CAST(:e AS uuid)"
                " AND work_date >= :a AND work_date < :b ORDER BY work_date DESC"
            ),
            {"e": employee_id, "a": first, "b": nxt},
        )
        .mappings()
        .all()
    )
    exc = _exceptions_for(conn, [str(r["attendance_id"]) for r in rows])
    return [_day_view(r, exc[str(r["attendance_id"])]) for r in rows]


def _month_days(first: date, nxt: date) -> list[date]:
    out, day = [], first
    while day < nxt:
        out.append(day)
        day += timedelta(days=1)
    return out


def _leave_by_person(conn: Connection, first: date, nxt: date) -> dict[str, dict[date, float]]:
    """Approved leave, person by person and day by day (a half day is 0.5)."""
    out: dict[str, dict[date, float]] = {}
    rows = conn.execute(
        text(
            "SELECT employee_id, from_date, to_date, half_day FROM hr.leave_request"
            " WHERE status = 'APPROVED' AND from_date < :b AND to_date >= :a"
        ),
        {"a": first, "b": nxt},
    )
    for employee_id, start, finish, half in rows:
        days = out.setdefault(str(employee_id), {})
        day = max(start, first)
        while day <= min(finish, nxt - timedelta(days=1)):
            days[day] = 0.5 if half else 1.0
            day += timedelta(days=1)
    return out


@router.get("/attendance/team")
def team_attendance(
    month: Annotated[str | None, Query(max_length=7)] = None,
    _: HumanPrincipal = Depends(can_read_all),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """HR and the CEO: the month at a glance. The month's working days and holidays, and for every
    active employee the days present, on leave and absent, work on a day off, and what waits for approval."""
    today = ist_date(clock())
    first, nxt = _month_bounds(month, today)
    holidays = {
        r[0]: str(r[1])
        for r in conn.execute(
            text(
                "SELECT holiday_date, name FROM hr.holiday WHERE status = 'DECLARED'"
                " AND holiday_date >= :a AND holiday_date < :b ORDER BY holiday_date"
            ),
            {"a": first, "b": nxt},
        )
    }
    days = _month_days(first, nxt)
    off = {d for d in days if is_sunday(d) or d in holidays}
    working = [d for d in days if d not in off]
    working_set = set(working)
    present: dict[str, set[date]] = {}
    for employee_id, work_date in conn.execute(
        text(
            "SELECT employee_id, work_date FROM hr.attendance_day"
            " WHERE check_in_at IS NOT NULL AND work_date >= :a AND work_date < :b"
        ),
        {"a": first, "b": nxt},
    ):
        present.setdefault(str(employee_id), set()).add(work_date)
    leave = _leave_by_person(conn, first, nxt)
    people = conn.execute(
        text(
            "SELECT e.employee_id, e.employee_code, e.full_name, e.date_of_joining,"
            " (SELECT count(*) FROM hr.attendance_exception x"
            "   WHERE x.employee_id = e.employee_id AND x.status = 'PENDING'"
            "     AND x.work_date >= :a AND x.work_date < :b) AS pending"
            " FROM hr.employee e WHERE e.employment_status = 'ACTIVE'"
            " ORDER BY lower(e.full_name), e.employee_code"
        ),
        {"a": first, "b": nxt},
    ).mappings()
    employees = []
    for r in people:
        eid = str(r["employee_id"])
        here = present.get(eid, set())
        mine = leave.get(eid, {})
        joined = r["date_of_joining"]
        counted = [d for d in working if d < today and (joined is None or d >= joined)]
        absent = sum(1 - mine.get(d, 0.0) for d in counted if d not in here)
        employees.append(
            {
                "employeeId": eid,
                "employeeCode": r["employee_code"],
                "fullName": r["full_name"],
                "daysPresent": sum(1 for d in working if d in here),
                "offDayWorked": sum(1 for d in off if d in here),
                "daysOnLeave": sum(v for d, v in mine.items() if d in working_set and d <= today),
                "daysAbsent": absent,
                "daysCheckedIn": len(here),
                "daysCheckedOut": 0,
                "pendingExceptions": r["pending"],
            }
        )
    out_counts = conn.execute(
        text(
            "SELECT employee_id, count(*) FROM hr.attendance_day WHERE check_out_at IS NOT NULL"
            " AND work_date >= :a AND work_date < :b GROUP BY employee_id"
        ),
        {"a": first, "b": nxt},
    )
    by_out = {str(e): int(n) for e, n in out_counts}
    for item in employees:
        item["daysCheckedOut"] = by_out.get(item["employeeId"], 0)
    return {
        "month": first.strftime("%Y-%m"),
        "summary": {
            "workingDays": len(working),
            "workingDaysSoFar": sum(1 for d in working if d <= today),
            "sundays": sum(1 for d in days if is_sunday(d)),
            "holidays": [
                {"date": d.isoformat(), "name": name} for d, name in sorted(holidays.items())
            ],
        },
        "employees": employees,
    }


@router.get("/attendance/employee/{employee_id}")
def employee_attendance(
    employee_id: str,
    month: Annotated[str | None, Query(max_length=7)] = None,
    _: HumanPrincipal = Depends(can_read_all),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    from hrmgmt.api.employees import _uuid

    eid = _uuid(employee_id)
    if (
        conn.execute(
            text("SELECT 1 FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"), {"e": eid}
        ).first()
        is None
    ):
        raise not_found("Employee not found.")
    first, nxt = _month_bounds(month, ist_date(clock()))
    return {"month": first.strftime("%Y-%m"), "days": _days(conn, eid, first, nxt)}


@router.get("/attendance/{attendance_id}/photo/{event}")
def attendance_photo(
    attendance_id: str,
    event: Literal["in", "out"],
    request: Request,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    storage: ObjectStorage | None = Depends(get_storage),
    conn: Connection = Depends(get_conn),
) -> Response:
    """The stamped photo, to the employee, to the Team Lead or Project Manager who decides on
    them, and to HR. Every view by someone other than the employee is written to the history."""
    from hrmgmt.api.employees import _uuid

    aid = _uuid(attendance_id)
    col = "check_in_photo_key" if event == "in" else "check_out_photo_key"
    row = (
        conn.execute(
            text(
                f"SELECT employee_id, {col} AS key FROM hr.attendance_day WHERE attendance_id = CAST(:a AS uuid)"
            ),
            {"a": aid},
        )
        .mappings()
        .first()
    )
    if row is None or not row["key"]:
        raise not_found("No photo.")
    employee_id = str(row["employee_id"])
    own = wc.employee_user_id(conn, employee_id) == user.user_id
    if not own:
        allowed = has_permission(
            authorizer, user, perm.HR_ATTENDANCE_READ_ALL
        ) or decides_attendance(conn, authorizer, user, employee_id, clock())
        if not allowed:
            raise not_found("No photo.")
        record_audit(
            conn,
            actor_user_id=user.user_id,
            action="ATTENDANCE_PHOTO_VIEWED",
            entity_type="employee",
            entity_id=employee_id,
            changes={"attendanceId": aid, "event": event},
            request=request,
        )
    if storage is None:
        raise dependency_unavailable("Photo storage is not configured.")
    try:
        data = storage.get(row["key"])
    except StorageError as exc:
        raise dependency_unavailable("The photo could not be loaded.") from exc
    return Response(
        content=data, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"}
    )


# ---- approvals -----------------------------------------------------------------------------


@router.get("/approvals/attendance")
def pending_attendance_exceptions(
    status: Annotated[Literal["PENDING", "APPROVED", "REJECTED"], Query()] = "PENDING",
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Late, early and location exceptions this person may decide on."""
    now = clock()
    rows = (
        conn.execute(
            text(
                """
            SELECT x.exception_id, x.attendance_id, x.employee_id, x.work_date, x.event, x.kind,
                   x.reason, x.status, x.decision_note, x.decided_at,
                   e.full_name, e.employee_code,
                   a.check_in_at, a.check_out_at, a.check_in_distance_m, a.check_out_distance_m,
                   a.check_in_outlet_name, a.check_out_outlet_name
            FROM hr.attendance_exception x
            JOIN hr.employee e ON e.employee_id = x.employee_id
            JOIN hr.attendance_day a ON a.attendance_id = x.attendance_id
            WHERE x.status = :s
            ORDER BY x.work_date DESC, x.created_at
            LIMIT 300
            """
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
        if r["kind"] == "NO_OUTLET_LOCATION":
            if not decides_attendance(conn, authorizer, user, eid, now, r["kind"]):
                continue
        elif not decides[eid]:
            continue
        side = "check_in" if r["event"] == "CHECK_IN" else "check_out"
        items.append(
            {
                "exceptionId": str(r["exception_id"]),
                "attendanceId": str(r["attendance_id"]),
                "employeeId": eid,
                "employeeCode": r["employee_code"],
                "employeeName": r["full_name"],
                "workDate": r["work_date"].isoformat(),
                "event": r["event"],
                "kind": r["kind"],
                "reason": r["reason"],
                "status": r["status"],
                "decisionNote": r["decision_note"],
                "at": r[f"{side}_at"].isoformat() if r[f"{side}_at"] else None,
                "distanceM": float(r[f"{side}_distance_m"])
                if r[f"{side}_distance_m"] is not None
                else None,
                "outletName": r[f"{side}_outlet_name"],
            }
        )
    return {"items": items}


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["APPROVE", "REJECT"]
    note: str | None = Field(default=None, max_length=500)


@router.post("/approvals/attendance/{exception_id}/decision")
def decide_attendance_exception(
    exception_id: str,
    body: Decision,
    request: Request,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    from hrmgmt.api.employees import _uuid

    xid = _uuid(exception_id)
    row = (
        conn.execute(
            text(
                "SELECT employee_id, status, work_date, kind FROM hr.attendance_exception"
                " WHERE exception_id = CAST(:x AS uuid) FOR UPDATE"
            ),
            {"x": xid},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Request not found.")
    employee_id = str(row["employee_id"])
    if not decides_attendance(
        conn, authorizer, user, employee_id, clock(), row["kind"], hr_override=True
    ):
        raise not_found("Request not found.")  # not shown to anyone who may not decide it
    if row["status"] != "PENDING":
        raise conflict("APPROVAL_ALREADY_DECIDED", "This request has already been decided.")
    if body.decision == "REJECT" and not (body.note and body.note.strip()):
        raise ApiError(422, "APPROVAL_NOTE_REQUIRED", "Say why you are rejecting it.")
    status = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
    conn.execute(
        text(
            "UPDATE hr.attendance_exception SET status = :s, decided_by = :u, decided_at = :n,"
            " decision_note = :note WHERE exception_id = CAST(:x AS uuid)"
        ),
        {
            "s": status,
            "u": user.user_id,
            "n": clock(),
            "note": (body.note or "").strip() or None,
            "x": xid,
        },
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="ATTENDANCE_EXCEPTION_" + status,
        entity_type="employee",
        entity_id=employee_id,
        changes={"exceptionId": xid, "workDate": row["work_date"].isoformat()},
        request=request,
    )
    return {"exceptionId": xid, "status": status}
