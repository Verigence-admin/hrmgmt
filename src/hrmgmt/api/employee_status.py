"""Changing an employee's status (Active, Suspended, Terminated, Quit).

HR asks for the change and the CEO approves it; the status changes only when it is approved. The
person who asked cannot approve it. One exception: a suspension dated today or earlier takes effect
at once when HR asks (it is recorded, and the login is suspended the same way as after an approval). On approval of a status that is not Active, the employee's
Verigence login is suspended at once (one attempt; the Sync with Verigence does it later if that
attempt fails). A login is never reactivated from here: Security keeps that for SuperAdmin."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from hrmgmt import permissions as perm
from hrmgmt import validators as v
from hrmgmt.api.employees import EmploymentStatus, get_provisioner
from hrmgmt.audit import record_audit
from hrmgmt.db import get_conn
from hrmgmt.errors import ApiError, conflict, forbidden, not_found
from hrmgmt.principal import require_permission
from hrmgmt.provisioning import ProvisioningError, UserProvisioner
from hrmgmt.security import HumanPrincipal
from hrmgmt.timeutil import Clock, ist_date, utc_now

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/hr/v1", tags=["Employee status"])

can_read = require_permission(perm.HR_EMPLOYEE_READ)
can_manage = require_permission(perm.HR_EMPLOYEE_MANAGE)
can_approve = require_permission(perm.HR_EMPLOYEE_STATUS_APPROVE)

# A change may be dated a little back (HR records it after the fact) or a little ahead.
_DAYS_BACK = 90
_DAYS_AHEAD = 60

_COLUMNS = (
    "c.change_id, c.employee_id, e.employee_code, e.full_name, c.from_status, c.to_status,"
    " c.effective_date, c.reason, c.status, c.requested_by, c.requested_at, c.decided_by,"
    " c.decided_at, c.decision_note, c.login_outcome"
)
_FROM = "FROM hr.employee_status_change c JOIN hr.employee e ON e.employee_id = c.employee_id"


def get_clock(request: Request) -> Clock:
    return getattr(request.app.state, "clock", None) or utc_now


class StatusChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    to_status: EmploymentStatus
    effective_date: date | None = None
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        cleaned = v.clean_text(value) or ""
        if len(cleaned) < 3:
            raise ValueError("Give a reason of at least 3 letters")
        return cleaned


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    note: str | None = Field(default=None, max_length=500)


def _view(row: Any) -> dict[str, Any]:
    return {
        "changeId": str(row["change_id"]),
        "employeeId": str(row["employee_id"]),
        "employeeCode": row["employee_code"],
        "employeeName": row["full_name"],
        "fromStatus": row["from_status"],
        "toStatus": row["to_status"],
        "effectiveDate": row["effective_date"].isoformat(),
        "reason": row["reason"],
        "status": row["status"],
        "requestedBy": row["requested_by"],
        "requestedAt": row["requested_at"].isoformat(),
        "decidedBy": row["decided_by"],
        "decidedAt": row["decided_at"].isoformat() if row["decided_at"] else None,
        "decisionNote": row["decision_note"],
        "loginOutcome": row["login_outcome"],
    }


def _one(conn: Connection, change_id: str) -> Any:
    row = (
        conn.execute(
            text(f"SELECT {_COLUMNS} {_FROM} WHERE c.change_id = CAST(:id AS uuid)"),
            {"id": change_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("This request was not found.")
    return row


def _uuid(value: str) -> str:
    import uuid

    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise not_found("Not found.") from exc


@router.post("/employees/{employee_id}/status-change", status_code=201)
def request_status_change(
    employee_id: str,
    body: StatusChangeIn,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    clock: Clock = Depends(get_clock),
    provisioner: UserProvisioner | None = Depends(get_provisioner),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """HR asks for a status change. Nothing changes until the CEO approves it, except a suspension
    dated today or earlier, which takes effect at once."""
    employee_id = _uuid(employee_id)
    found = (
        conn.execute(
            text(
                "SELECT employment_status, security_user_id::text AS uid FROM hr.employee"
                " WHERE employee_id = CAST(:id AS uuid)"
            ),
            {"id": employee_id},
        )
        .mappings()
        .first()
    )
    if found is None:
        raise not_found("Employee not found.")
    current = found["employment_status"]
    if current == body.to_status:
        raise conflict("STATUS_UNCHANGED", "The employee already has this status.")
    today = ist_date(clock())
    effective = body.effective_date or today
    if effective < today - timedelta(days=_DAYS_BACK) or effective > today + timedelta(
        days=_DAYS_AHEAD
    ):
        raise ApiError(
            422,
            "STATUS_DATE_NOT_VALID",
            f"The date must be within {_DAYS_BACK} days back and {_DAYS_AHEAD} days ahead.",
        )
    try:
        change_id = str(
            conn.execute(
                text(
                    "INSERT INTO hr.employee_status_change"
                    " (employee_id, from_status, to_status, effective_date, reason, requested_by)"
                    " VALUES (CAST(:e AS uuid), :f, :t, :d, :r, :u) RETURNING change_id"
                ),
                {
                    "e": employee_id,
                    "f": current,
                    "t": body.to_status,
                    "d": effective,
                    "r": body.reason,
                    "u": user.user_id,
                },
            ).scalar_one()
        )
    except IntegrityError as exc:
        conn.rollback()
        raise conflict(
            "STATUS_CHANGE_PENDING",
            "A change for this employee is already waiting for approval.",
        ) from exc
    if body.to_status == "SUSPENDED" and effective <= today:
        return _suspend_now(
            conn,
            request,
            user.user_id,
            change_id,
            employee_id,
            found["uid"],
            current,
            effective,
            body.reason,
            provisioner,
        )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="STATUS_CHANGE_REQUESTED",
        entity_type="employee",
        entity_id=employee_id,
        changes={
            "status": {"from": current, "to": body.to_status},
            "effectiveDate": effective.isoformat(),
            "reason": body.reason,
        },
        request=request,
    )
    return _view(_one(conn, change_id))


def _suspend_now(
    conn: Connection,
    request: Request,
    actor: str,
    change_id: str,
    employee_id: str,
    login_user_id: str | None,
    from_status: str,
    effective: date,
    reason: str,
    provisioner: UserProvisioner | None,
) -> dict[str, Any]:
    """HR suspends: the status changes now, the request is recorded as decided by the person who made
    it, and the Verigence login is suspended with the one attempt every status change makes."""
    conn.execute(
        text(
            "UPDATE hr.employee SET employment_status = 'SUSPENDED', updated_at = now(), updated_by = :u"
            " WHERE employee_id = CAST(:id AS uuid)"
        ),
        {"u": actor, "id": employee_id},
    )
    conn.execute(
        text(
            "UPDATE hr.employee_status_change SET status = 'APPROVED', decided_by = :u, decided_at = now(),"
            " decision_note = 'Suspension applied directly by HR' WHERE change_id = CAST(:id AS uuid)"
        ),
        {"u": actor, "id": change_id},
    )
    record_audit(
        conn,
        actor_user_id=actor,
        action="EMPLOYEE_STATUS_CHANGED",
        entity_type="employee",
        entity_id=employee_id,
        changes={
            "status": {"from": from_status, "to": "SUSPENDED"},
            "effectiveDate": effective.isoformat(),
            "reason": reason,
            "direct": True,
        },
        request=request,
    )
    conn.commit()  # the suspension stands even if the login step below does not work
    outcome = _login_step(provisioner, login_user_id, "SUSPENDED")
    conn.execute(
        text(
            "UPDATE hr.employee_status_change SET login_outcome = :o WHERE change_id = CAST(:id AS uuid)"
        ),
        {"o": outcome, "id": change_id},
    )
    return _view(_one(conn, change_id))


@router.get("/employee-status-changes")
def list_status_changes(
    status: Annotated[str | None, Query(pattern="^(PENDING|APPROVED|REJECTED|CANCELLED)$")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    _: HumanPrincipal = Depends(can_read),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    rows = (
        conn.execute(
            text(
                f"SELECT {_COLUMNS} {_FROM}"
                " WHERE (CAST(:s AS text) IS NULL OR c.status = :s)"
                " ORDER BY c.requested_at DESC LIMIT :n"
            ),
            {"s": status, "n": limit},
        )
        .mappings()
        .all()
    )
    return {"items": [_view(r) for r in rows]}


@router.get("/employees/{employee_id}/status-changes")
def employee_status_history(
    employee_id: str,
    _: HumanPrincipal = Depends(can_read),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    rows = (
        conn.execute(
            text(
                f"SELECT {_COLUMNS} {_FROM} WHERE c.employee_id = CAST(:id AS uuid)"
                " ORDER BY c.requested_at DESC LIMIT 100"
            ),
            {"id": _uuid(employee_id)},
        )
        .mappings()
        .all()
    )
    return {"items": [_view(r) for r in rows]}


def _locked_open(conn: Connection, change_id: str) -> Any:
    row = (
        conn.execute(
            text(
                "SELECT change_id, employee_id, from_status, to_status, status, requested_by"
                " FROM hr.employee_status_change WHERE change_id = CAST(:id AS uuid) FOR UPDATE"
            ),
            {"id": change_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("This request was not found.")
    if row["status"] != "PENDING":
        raise conflict("STATUS_CHANGE_DECIDED", "This request has already been decided.")
    return row


@router.post("/employee-status-changes/{change_id}/approve")
def approve_status_change(
    change_id: str,
    request: Request,
    body: DecisionIn | None = None,
    user: HumanPrincipal = Depends(can_approve),
    provisioner: UserProvisioner | None = Depends(get_provisioner),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """The CEO approves: the employee's status changes now. The person who asked cannot approve."""
    change_id = _uuid(change_id)
    row = _locked_open(conn, change_id)
    if row["requested_by"] == user.user_id:
        raise forbidden("You cannot approve your own request. Another approver must do it.")
    employee = (
        conn.execute(
            text(
                "SELECT employment_status, security_user_id::text AS uid FROM hr.employee"
                " WHERE employee_id = CAST(:id AS uuid) FOR UPDATE"
            ),
            {"id": str(row["employee_id"])},
        )
        .mappings()
        .one()
    )
    if employee["employment_status"] != row["from_status"]:
        raise conflict(
            "STATUS_CHANGED_MEANWHILE",
            "The employee's status is no longer what it was when this was requested. "
            "Reject this request and ask again.",
        )
    conn.execute(
        text(
            "UPDATE hr.employee SET employment_status = :t, updated_at = now(), updated_by = :u"
            " WHERE employee_id = CAST(:id AS uuid)"
        ),
        {"t": row["to_status"], "u": user.user_id, "id": str(row["employee_id"])},
    )
    conn.execute(
        text(
            "UPDATE hr.employee_status_change SET status = 'APPROVED', decided_by = :u,"
            " decided_at = now(), decision_note = :n WHERE change_id = CAST(:id AS uuid)"
        ),
        {"u": user.user_id, "n": v.clean_text(body.note) if body else None, "id": change_id},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="EMPLOYEE_STATUS_CHANGED",
        entity_type="employee",
        entity_id=str(row["employee_id"]),
        changes={"status": {"from": row["from_status"], "to": row["to_status"]}},
        request=request,
    )
    conn.commit()  # the approval stands even if the login step below does not work

    outcome = _login_step(provisioner, employee["uid"], row["to_status"])
    conn.execute(
        text(
            "UPDATE hr.employee_status_change SET login_outcome = :o"
            " WHERE change_id = CAST(:id AS uuid)"
        ),
        {"o": outcome, "id": change_id},
    )
    return _view(_one(conn, change_id))


def _login_step(provisioner: UserProvisioner | None, user_id: str | None, to_status: str) -> str:
    """What happened to the Verigence login. One attempt. Never reactivates."""
    if not user_id:
        return "NO_LOGIN"
    if provisioner is None:
        return "LOGIN_NOT_CHECKED"
    try:
        if to_status != "ACTIVE":
            outcomes = provisioner.sync_employees(items=[(user_id, True)])
            done = outcomes[0] if outcomes else None
            if done is None or not done.found:
                return "LOGIN_NOT_FOUND"
            if done.suspended:
                return "LOGIN_SUSPENDED"
            if done.note == "SUPER_ADMIN_PROTECTED":
                return "LOGIN_SUPERADMIN_NOT_SUSPENDED"
            return "LOGIN_ALREADY_NOT_ACTIVE"
        found = provisioner.list_users(ids=[user_id], limit=1)
        if not found:
            return "LOGIN_NOT_FOUND"
        return "LOGIN_ACTIVE" if found[0].status == "ACTIVE" else "LOGIN_NEEDS_SUPERADMIN"
    except ProvisioningError as exc:
        logger.warning("hr_status_login_step_failed", code=exc.code)
        return "LOGIN_NOT_UPDATED"


@router.post("/employee-status-changes/{change_id}/reject")
def reject_status_change(
    change_id: str,
    body: DecisionIn,
    request: Request,
    user: HumanPrincipal = Depends(can_approve),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    change_id = _uuid(change_id)
    note = v.clean_text(body.note)
    if not note or len(note) < 3:
        raise ApiError(422, "STATUS_NOTE_REQUIRED", "Say why the request is rejected.")
    row = _locked_open(conn, change_id)
    conn.execute(
        text(
            "UPDATE hr.employee_status_change SET status = 'REJECTED', decided_by = :u,"
            " decided_at = now(), decision_note = :n WHERE change_id = CAST(:id AS uuid)"
        ),
        {"u": user.user_id, "n": note, "id": change_id},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="STATUS_CHANGE_REJECTED",
        entity_type="employee",
        entity_id=str(row["employee_id"]),
        changes={"status": {"from": row["from_status"], "to": row["to_status"]}, "note": note},
        request=request,
    )
    return _view(_one(conn, change_id))


@router.post("/employee-status-changes/{change_id}/cancel")
def cancel_status_change(
    change_id: str,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """The person who asked takes the request back before it is decided."""
    change_id = _uuid(change_id)
    row = _locked_open(conn, change_id)
    if row["requested_by"] != user.user_id:
        raise forbidden("Only the person who asked can cancel this request.")
    conn.execute(
        text(
            "UPDATE hr.employee_status_change SET status = 'CANCELLED', decided_by = :u,"
            " decided_at = now() WHERE change_id = CAST(:id AS uuid)"
        ),
        {"u": user.user_id, "id": change_id},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="STATUS_CHANGE_CANCELLED",
        entity_type="employee",
        entity_id=str(row["employee_id"]),
        changes={"status": {"from": row["from_status"], "to": row["to_status"]}},
        request=request,
    )
    return _view(_one(conn, change_id))
