from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from hrmgmt import holidays as hol
from hrmgmt import leave_rules as lr
from hrmgmt import permissions as perm
from hrmgmt.api.attendance import get_clock
from hrmgmt.api.employees import _own_employee_id, _uuid
from hrmgmt.approvals import decides_leave, leave_rule
from hrmgmt.audit import record_audit
from hrmgmt.authz import Authorizer
from hrmgmt.db import get_conn
from hrmgmt.errors import ApiError, conflict, not_found
from hrmgmt.principal import current_user, get_authorizer, require_permission
from hrmgmt.security import HumanPrincipal
from hrmgmt.timeutil import Clock, ist_date

router = APIRouter(prefix="/hr/v1", tags=["Leave"])

can_review = require_permission(perm.HR_LEAVE_REVIEW)

LeaveType = Literal["PAID", "SPECIAL", "COMP_OFF", "UNPAID"]
_BACKDATE_DAYS = 30
_ADVANCE_DAYS = 365
_LABELS = {"PAID": "paid leave", "SPECIAL": "birthday leave", "COMP_OFF": "comp-off"}


class LeaveApply(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    leave_type: LeaveType
    from_date: date
    to_date: date
    half_day: bool = False
    reason: str | None = Field(default=None, max_length=500)
    # Special occasion leave is birthday leave (7 Nov 2026): the server records "Birthday" and
    # ignores what is sent here. Kept so earlier app versions, which still send it, are accepted.
    occasion: str | None = Field(default=None, max_length=100)


def _num(value: Decimal | None) -> float:
    return float(value) if value is not None else 0.0


def _view(r: Any) -> dict[str, Any]:
    return {
        "requestId": str(r["request_id"]),
        "leaveType": r["leave_type"],
        "fromDate": r["from_date"].isoformat(),
        "toDate": r["to_date"].isoformat(),
        "halfDay": r["half_day"],
        "days": _num(r["days"]),
        "reason": r["reason"],
        "occasion": r["occasion"],
        "status": r["status"],
        "approverRule": r["approver_rule"],
        "submittedAt": r["submitted_at"].isoformat(),
        "decidedAt": r["decided_at"].isoformat() if r["decided_at"] else None,
        "decisionNote": r["decision_note"],
    }


def _balance_view(conn: Connection, employee_id: str, year: int, today: date) -> dict[str, Any]:
    b = lr.balances(conn, employee_id, year, today)
    return {
        "year": year,
        # SPECIAL is birthday leave: its figures are for the birthday window holding today.
        "birthdayLeave": lr.special_info(conn, employee_id, today),
        "types": [
            {
                "leaveType": t,
                "granted": _num(v["granted"]),
                "used": _num(v["used"]),
                "pending": _num(v["pending"]),
                "balance": _num(v["balance"]),
                "available": _num(v["balance"] - v["pending"]),
            }
            for t, v in b.items()
        ],
    }


@router.get("/leave/balance")
def my_balance(
    year: Annotated[int | None, Query(ge=2020, le=2100)] = None,
    user: HumanPrincipal = Depends(current_user),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    today = ist_date(clock())
    return _balance_view(conn, employee_id, year or today.year, today)


@router.get("/leave/requests")
def my_requests(
    user: HumanPrincipal = Depends(current_user), conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    rows = conn.execute(
        text(
            "SELECT * FROM hr.leave_request WHERE employee_id = CAST(:e AS uuid)"
            " ORDER BY from_date DESC, submitted_at DESC LIMIT 100"
        ),
        {"e": employee_id},
    ).mappings()
    return {"items": [_view(r) for r in rows]}


@router.get("/leave/earning")
def my_earning(
    user: HumanPrincipal = Depends(current_user),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """How many days of attendance count so far towards the next paid leave, and what each
    completed block earned (or lost to the cap)."""
    employee_id = _own_employee_id(conn, user)
    today = ist_date(clock())
    lr.accrue(conn, employee_id, today)
    return lr.progress(conn, employee_id, today)


@router.get("/leave/absences")
def my_absences(
    user: HumanPrincipal = Depends(current_user),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Working days in the last 30 with no check-in and no leave: each can still be applied for as
    leave, so it is not left unexplained."""
    employee_id = _own_employee_id(conn, user)
    return {"dates": [d.isoformat() for d in unexplained_absences(conn, employee_id, clock())]}


def unexplained_absences(conn: Connection, employee_id: str, now: datetime) -> list[date]:
    today = ist_date(now)
    start = today - timedelta(days=_BACKDATE_DAYS)
    first = conn.execute(
        text("SELECT min(work_date) FROM hr.attendance_day WHERE employee_id = CAST(:e AS uuid)"),
        {"e": employee_id},
    ).scalar()
    if first is None:
        return []  # nobody is marked absent before they ever used attendance
    start = max(start, first)
    end = today - timedelta(days=1)
    if end < start:
        return []
    present = {
        r[0]
        for r in conn.execute(
            text(
                "SELECT work_date FROM hr.attendance_day WHERE employee_id = CAST(:e AS uuid)"
                " AND check_in_at IS NOT NULL AND work_date BETWEEN :a AND :b"
            ),
            {"e": employee_id, "a": start, "b": end},
        )
    }
    on_leave: set[date] = set()
    for a, b in conn.execute(
        text(
            "SELECT from_date, to_date FROM hr.leave_request WHERE employee_id = CAST(:e AS uuid)"
            " AND status IN ('PENDING', 'APPROVED') AND from_date <= :b AND to_date >= :a"
        ),
        {"e": employee_id, "a": start, "b": end},
    ):
        d = a
        while d <= b:
            on_leave.add(d)
            d += timedelta(days=1)
    # A half-day holiday still has a working half: with no check-in and no leave it is unexplained.
    holidays = hol.holidays_for(conn, employee_id, start, end)
    out = []
    d = start
    while d <= end:
        if not hol.is_off_day(d, holidays.get(d)) and d not in present and d not in on_leave:
            out.append(d)
        d += timedelta(days=1)
    return out


@router.post("/leave/requests", status_code=201)
def apply_leave(
    body: LeaveApply,
    request: Request,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    now = clock()
    today = ist_date(now)
    if body.from_date < today - timedelta(days=_BACKDATE_DAYS):
        raise ApiError(
            422,
            "LEAVE_DATES_INVALID",
            f"Leave can be applied up to {_BACKDATE_DAYS} days after the fact.",
        )
    if body.to_date > today + timedelta(days=_ADVANCE_DAYS):
        raise ApiError(422, "LEAVE_DATES_INVALID", "That is too far ahead.")
    days = lr.leave_days(conn, body.from_date, body.to_date, body.half_day, employee_id)
    occasion = None
    special_year: int | None = None
    if body.leave_type == "SPECIAL":
        # Birthday leave only (product owner, 7 Nov 2026): from the birthday to the day before the
        # next one, never before it.
        occasion = lr.BIRTHDAY
        special_year = lr.special_window(conn, employee_id, body.from_date, body.to_date)

    # One request at a time per person, so two quick submissions cannot both pass the checks.
    conn.execute(
        text("SELECT employee_id FROM hr.employee WHERE employee_id = CAST(:e AS uuid) FOR UPDATE"),
        {"e": employee_id},
    )
    clash = conn.execute(
        text(
            "SELECT 1 FROM hr.leave_request WHERE employee_id = CAST(:e AS uuid)"
            " AND status IN ('PENDING', 'APPROVED') AND from_date <= :b AND to_date >= :a LIMIT 1"
        ),
        {"e": employee_id, "a": body.from_date, "b": body.to_date},
    ).first()
    if clash:
        raise conflict("LEAVE_OVERLAPS", "You already have leave on some of these dates.")
    if body.leave_type in lr.PAID:
        info = lr.balances(
            conn, employee_id, body.from_date.year, today, special_on=body.from_date
        )[body.leave_type]
        available = info["balance"] - info["pending"]
        if days > available and special_year is not None:
            dob = lr.date_of_birth(conn, employee_id)
            assert dob is not None
            raise conflict("LEAVE_BALANCE_TOO_LOW", lr.special_used_message(dob, special_year))
        if days > available:
            raise conflict(
                "LEAVE_BALANCE_TOO_LOW",
                f"You have {available:g} day(s) of {_LABELS[body.leave_type]} available. "
                "Apply for unpaid leave for the rest.",
            )
    rule = leave_rule(conn, authorizer, employee_id, body.leave_type, now)
    status = "APPROVED" if rule == "AUTO" else "PENDING"
    request_id = str(
        conn.execute(
            text(
                "INSERT INTO hr.leave_request (employee_id, leave_type, from_date, to_date, half_day,"
                " days, reason, occasion, status, approver_rule, submitted_at, decided_at, decided_by,"
                " decision_note) VALUES (CAST(:e AS uuid), :t, :a, :b, :h, :d, :r, :o, :s, :rule,"
                " :now, :da, :db, :dn)"
                " RETURNING request_id"
            ),
            {
                "e": employee_id,
                "t": body.leave_type,
                "a": body.from_date,
                "b": body.to_date,
                "h": body.half_day,
                "d": days,
                "r": (body.reason or "").strip() or None,
                "o": occasion,
                "s": status,
                "rule": rule,
                "now": now,
                "da": now if status == "APPROVED" else None,
                "db": user.user_id if status == "APPROVED" else None,
                "dn": "Recorded without approval (CEO)." if status == "APPROVED" else None,
            },
        ).scalar_one()
    )
    if status == "APPROVED" and body.leave_type in lr.PAID:
        _deduct(
            conn,
            employee_id,
            body.leave_type,
            special_year if special_year is not None else body.from_date.year,
            days,
            request_id,
            user.user_id,
        )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="LEAVE_APPLIED",
        entity_type="employee",
        entity_id=employee_id,
        changes={
            "requestId": request_id,
            "type": body.leave_type,
            "days": float(days),
            "rule": rule,
        },
        request=request,
    )
    row = (
        conn.execute(
            text("SELECT * FROM hr.leave_request WHERE request_id = CAST(:r AS uuid)"),
            {"r": request_id},
        )
        .mappings()
        .one()
    )
    return {
        **_view(row),
        "recentLeave": lr.recent_leave(conn, employee_id, body.from_date, today),
    }


@router.get("/leave/recent")
def my_recent_leave(
    from_date: Annotated[date, Query(alias="fromDate")],
    user: HumanPrincipal = Depends(current_user),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """While applying: paid and unpaid leave days approved in the 6 weeks before the chosen from
    date (product owner, 7 Nov 2026). The approver sees the same numbers with the request."""
    return lr.recent_leave(conn, _own_employee_id(conn, user), from_date, ist_date(clock()))


def _deduct(
    conn: Connection,
    employee_id: str,
    leave_type: str,
    year: int,
    days: Decimal,
    request_id: str,
    actor: str,
) -> None:
    conn.execute(
        text(
            "INSERT INTO hr.leave_ledger (employee_id, leave_type, leave_year, entry_type, days, request_id, created_by)"
            " VALUES (CAST(:e AS uuid), :t, :y, 'DEDUCT', :d, CAST(:r AS uuid), :u)"
        ),
        {"e": employee_id, "t": leave_type, "y": year, "d": -days, "r": request_id, "u": actor},
    )


@router.post("/leave/requests/{request_id}/cancel")
def cancel_leave(
    request_id: str,
    request: Request,
    user: HumanPrincipal = Depends(current_user),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    rid = _uuid(request_id)
    row = conn.execute(
        text(
            "SELECT status FROM hr.leave_request WHERE request_id = CAST(:r AS uuid)"
            " AND employee_id = CAST(:e AS uuid) FOR UPDATE"
        ),
        {"r": rid, "e": employee_id},
    ).first()
    if row is None:
        raise not_found("Leave request not found.")
    if row[0] != "PENDING":
        raise conflict(
            "LEAVE_NOT_PENDING",
            "Only a pending request can be cancelled. Ask HR to reverse an approved one.",
        )
    conn.execute(
        text(
            "UPDATE hr.leave_request SET status = 'CANCELLED' WHERE request_id = CAST(:r AS uuid)"
        ),
        {"r": rid},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="LEAVE_CANCELLED",
        entity_type="employee",
        entity_id=employee_id,
        changes={"requestId": rid},
        request=request,
    )
    return {"requestId": rid, "status": "CANCELLED"}


# ---- approvals -----------------------------------------------------------------------------


@router.get("/approvals/leave")
def leave_approvals(
    status: Annotated[Literal["PENDING", "APPROVED", "REJECTED"], Query()] = "PENDING",
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    now = clock()
    rows = (
        conn.execute(
            text(
                "SELECT r.*, e.full_name, e.employee_code FROM hr.leave_request r"
                " JOIN hr.employee e ON e.employee_id = r.employee_id"
                " WHERE r.status = :s ORDER BY r.from_date, r.submitted_at LIMIT 300"
            ),
            {"s": status},
        )
        .mappings()
        .all()
    )
    items = []
    today = ist_date(now)
    for r in rows:
        if decides_leave(conn, authorizer, user, str(r["employee_id"]), r["approver_rule"], now):
            item = {
                **_view(r),
                "employeeId": str(r["employee_id"]),
                "employeeName": r["full_name"],
                "employeeCode": r["employee_code"],
            }
            if status == "PENDING":
                eid = str(r["employee_id"])
                if r["leave_type"] in lr.PAID:
                    b = lr.balances(
                        conn, eid, r["from_date"].year, today, special_on=r["from_date"]
                    )[r["leave_type"]]
                    item["balanceAvailable"] = _num(b["balance"])
                item["coverWarnings"] = cover_warnings(
                    conn, eid, r["from_date"], r["to_date"], r["half_day"], now
                )
                # Leave taken in the 6 weeks before this request's from date (7 Nov 2026).
                item["recentLeave"] = lr.recent_leave(conn, eid, r["from_date"], today)
            items.append(item)
    return {"items": items}


def cover_warnings(
    conn: Connection, employee_id: str, start: date, end: date, half_day: bool, now: datetime
) -> list[dict[str, Any]]:
    """For each outlet where this person is a PC: the working days of the leave on which no other
    PC of that outlet is free (every other one is on leave, approved or waiting). A half day leaves
    the person there for part of the day, so it never leaves an outlet uncovered."""
    if half_day:
        return []
    user_id = conn.execute(
        text("SELECT security_user_id FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"),
        {"e": employee_id},
    ).scalar()
    if user_id is None:
        return []
    outlets = conn.execute(
        text(
            "SELECT DISTINCT outlet_id, outlet_name FROM hr.work_assignment"
            " WHERE security_user_id = CAST(:u AS uuid) AND role_code = 'PC'"
            " AND outlet_id IS NOT NULL AND valid_from <= :n AND (valid_to IS NULL OR valid_to > :n)"
        ),
        {"u": str(user_id), "n": now},
    ).all()
    holidays = hol.holidays_for(conn, employee_id, start, end)
    days = []
    d = start
    while d <= end:
        if not hol.is_off_day(d, holidays.get(d)):
            days.append(d)
        d += timedelta(days=1)
    out = []
    for outlet_id, outlet_name in outlets:
        others = [
            str(r[0])
            for r in conn.execute(
                text(
                    "SELECT DISTINCT e.employee_id FROM hr.work_assignment w"
                    " JOIN hr.employee e ON e.security_user_id = w.security_user_id"
                    " WHERE w.outlet_id = CAST(:o AS uuid) AND w.role_code = 'PC'"
                    " AND w.valid_from <= :n AND (w.valid_to IS NULL OR w.valid_to > :n)"
                    " AND e.employee_id <> CAST(:e AS uuid) AND e.employment_status = 'ACTIVE'"
                ),
                {"o": str(outlet_id), "n": now, "e": employee_id},
            )
        ]
        away: dict[str, set[date]] = {o: set() for o in others}
        if others:
            for eid, a, b in conn.execute(
                text(
                    "SELECT CAST(employee_id AS text), from_date, to_date FROM hr.leave_request"
                    " WHERE employee_id = ANY(CAST(:ids AS uuid[])) AND NOT half_day"
                    " AND status IN ('PENDING', 'APPROVED') AND from_date <= :b AND to_date >= :a"
                ),
                {"ids": others, "a": start, "b": end},
            ):
                x = a
                while x <= b:
                    away[eid].add(x)
                    x += timedelta(days=1)
        uncovered = [day for day in days if all(day in away[o] for o in others)]
        if uncovered:
            out.append(
                {
                    "outletName": outlet_name,
                    "otherPcs": len(others),
                    "dates": [day.isoformat() for day in uncovered],
                }
            )
    return out


class LeaveDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["APPROVE", "REJECT"]
    note: str | None = Field(default=None, max_length=500)


@router.post("/approvals/leave/{request_id}/decision")
def decide_leave(
    request_id: str,
    body: LeaveDecision,
    request: Request,
    user: HumanPrincipal = Depends(current_user),
    authorizer: Authorizer = Depends(get_authorizer),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    rid = _uuid(request_id)
    row = (
        conn.execute(
            text("SELECT * FROM hr.leave_request WHERE request_id = CAST(:r AS uuid) FOR UPDATE"),
            {"r": rid},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Leave request not found.")
    employee_id = str(row["employee_id"])
    now = clock()
    if not decides_leave(conn, authorizer, user, employee_id, row["approver_rule"], now):
        raise not_found("Leave request not found.")
    if row["status"] != "PENDING":
        raise conflict("APPROVAL_ALREADY_DECIDED", "This request has already been decided.")
    if body.decision == "REJECT" and not (body.note and body.note.strip()):
        raise ApiError(422, "APPROVAL_NOTE_REQUIRED", "Say why you are rejecting it.")
    status = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
    if status == "APPROVED" and row["leave_type"] in lr.PAID:
        year = row["from_date"].year
        if row["leave_type"] == "SPECIAL":
            # The birthday rule is checked again: a request raised before it, or a date of birth
            # changed since, is refused with the same message the employee would get.
            year = lr.special_window(conn, employee_id, row["from_date"], row["to_date"])
        balance = lr.balances(conn, employee_id, year, ist_date(now), special_on=row["from_date"])[
            row["leave_type"]
        ]["balance"]
        if row["days"] > balance and row["leave_type"] == "SPECIAL":
            dob = lr.date_of_birth(conn, employee_id)
            assert dob is not None
            raise conflict("LEAVE_BALANCE_TOO_LOW", lr.special_used_message(dob, year))
        if row["days"] > balance:
            raise conflict(
                "LEAVE_BALANCE_TOO_LOW", "The balance is no longer enough for this leave."
            )
        _deduct(conn, employee_id, row["leave_type"], year, row["days"], rid, user.user_id)
    conn.execute(
        text(
            "UPDATE hr.leave_request SET status = :s, decided_by = :u, decided_at = :n, decision_note = :note"
            " WHERE request_id = CAST(:r AS uuid)"
        ),
        {
            "s": status,
            "u": user.user_id,
            "n": now,
            "note": (body.note or "").strip() or None,
            "r": rid,
        },
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="LEAVE_" + status,
        entity_type="employee",
        entity_id=employee_id,
        changes={"requestId": rid, "days": float(row["days"])},
        request=request,
    )
    return {"requestId": rid, "status": status}


# ---- HR --------------------------------------------------------------------------------------


@router.post("/leave/requests/{request_id}/reverse")
def reverse_leave(
    request_id: str,
    request: Request,
    user: HumanPrincipal = Depends(can_review),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """HR takes back an approved leave: the days return to the balance (a new ledger row)."""
    rid = _uuid(request_id)
    row = (
        conn.execute(
            text("SELECT * FROM hr.leave_request WHERE request_id = CAST(:r AS uuid) FOR UPDATE"),
            {"r": rid},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Leave request not found.")
    if row["status"] != "APPROVED":
        raise conflict("LEAVE_NOT_APPROVED", "Only an approved leave can be reversed.")
    if row["leave_type"] in lr.PAID + lr.LEGACY:
        # Back to the year the days were taken from (for birthday leave, the birthday's year).
        taken_year = conn.execute(
            text(
                "SELECT leave_year FROM hr.leave_ledger WHERE request_id = CAST(:r AS uuid)"
                " AND entry_type = 'DEDUCT' ORDER BY created_at LIMIT 1"
            ),
            {"r": rid},
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO hr.leave_ledger (employee_id, leave_type, leave_year, entry_type, days, request_id, note, created_by)"
                " VALUES (CAST(:e AS uuid), :t, :y, 'REVERSAL', :d, CAST(:r AS uuid), 'Reversed by HR', :u)"
            ),
            {
                "e": str(row["employee_id"]),
                "t": row["leave_type"],
                "y": taken_year if taken_year is not None else row["from_date"].year,
                "d": row["days"],
                "r": rid,
                "u": user.user_id,
            },
        )
    conn.execute(
        text(
            "UPDATE hr.leave_request SET status = 'CANCELLED' WHERE request_id = CAST(:r AS uuid)"
        ),
        {"r": rid},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="LEAVE_REVERSED",
        entity_type="employee",
        entity_id=str(row["employee_id"]),
        changes={"requestId": rid},
        request=request,
    )
    return {"requestId": rid, "status": "CANCELLED"}


class Adjustment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    leave_type: Literal["PAID", "SPECIAL", "COMP_OFF"]
    year: int = Field(ge=2020, le=2100)
    days: Decimal = Field(max_digits=4, decimal_places=1)
    note: str = Field(min_length=3, max_length=300)


@router.post("/leave/employee/{employee_id}/adjust")
def adjust_balance(
    employee_id: str,
    body: Adjustment,
    request: Request,
    user: HumanPrincipal = Depends(can_review),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    eid = _uuid(employee_id)
    if (
        conn.execute(
            text("SELECT 1 FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"), {"e": eid}
        ).first()
        is None
    ):
        raise not_found("Employee not found.")
    if body.days == 0:
        raise ApiError(422, "HR_VALIDATION_FAILED", "Enter the number of days to add or take off.")
    lr.ensure_grants(conn, eid, body.year)
    conn.execute(
        text(
            "INSERT INTO hr.leave_ledger (employee_id, leave_type, leave_year, entry_type, days, note, created_by)"
            " VALUES (CAST(:e AS uuid), :t, :y, 'ADJUST', :d, :n, :u)"
        ),
        {
            "e": eid,
            "t": body.leave_type,
            "y": body.year,
            "d": body.days,
            "n": body.note,
            "u": user.user_id,
        },
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="LEAVE_BALANCE_ADJUSTED",
        entity_type="employee",
        entity_id=eid,
        changes={
            "type": body.leave_type,
            "year": body.year,
            "days": float(body.days),
            "note": body.note,
        },
        request=request,
    )
    return _balance_view(conn, eid, body.year, ist_date(clock()))


@router.get("/leave/employee/{employee_id}")
def employee_leave(
    employee_id: str,
    year: Annotated[int | None, Query(ge=2020, le=2100)] = None,
    _: HumanPrincipal = Depends(can_review),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    eid = _uuid(employee_id)
    if (
        conn.execute(
            text("SELECT 1 FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"), {"e": eid}
        ).first()
        is None
    ):
        raise not_found("Employee not found.")
    rows = conn.execute(
        text(
            "SELECT * FROM hr.leave_request WHERE employee_id = CAST(:e AS uuid) ORDER BY from_date DESC LIMIT 100"
        ),
        {"e": eid},
    ).mappings()
    today = ist_date(clock())
    return {
        "balance": _balance_view(conn, eid, year or today.year, today),
        "earning": lr.progress(conn, eid, today),
        "requests": [
            {**_view(r), "recentLeave": lr.recent_leave(conn, eid, r["from_date"], today)}
            if r["status"] == "PENDING"
            else _view(r)
            for r in rows
        ],
    }


@router.get("/leave/overview")
def leave_overview(
    year: Annotated[int | None, Query(ge=2020, le=2100)] = None,
    _: HumanPrincipal = Depends(can_review),
    clock: Clock = Depends(get_clock),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """HR: every active employee's balances for the year."""
    today = ist_date(clock())
    y = year or today.year
    ids = [
        (str(r[0]), r[1], r[2])
        for r in conn.execute(
            text(
                "SELECT employee_id, employee_code, full_name FROM hr.employee WHERE employment_status = 'ACTIVE' ORDER BY lower(full_name), employee_code"
            )
        )
    ]
    items = []
    for eid, code, name in ids:
        b = _balance_view(conn, eid, y, today)
        items.append(
            {"employeeId": eid, "employeeCode": code, "fullName": name, "types": b["types"]}
        )
    return {"year": y, "items": items}
