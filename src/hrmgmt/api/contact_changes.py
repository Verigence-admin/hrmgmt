"""An employee asks HR to change their email or mobile.

The employee cannot edit these two themselves: they raise a request that carries the old and the new
value. HR approves or rejects it. Only on approval does the change happen, the same way as when HR
edits the employee: the Verigence login (same user, same password) is changed first, then HR's record.
The person who asked cannot approve their own request."""

from __future__ import annotations

from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from hrmgmt import permissions as perm
from hrmgmt import validators as v
from hrmgmt.api.employees import (
    _change_login_contact,
    _email_in_use,
    _fetch,
    _mobile_in_use,
    _own_employee_id,
    _uuid,
    get_provisioner,
)
from hrmgmt.audit import record_audit
from hrmgmt.db import get_conn
from hrmgmt.errors import ApiError, conflict, forbidden, not_found
from hrmgmt.principal import current_user, require_permission
from hrmgmt.provisioning import UserProvisioner
from hrmgmt.security import HumanPrincipal

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/hr/v1", tags=["Contact change requests"])

can_read = require_permission(perm.HR_EMPLOYEE_READ)
can_manage = require_permission(perm.HR_EMPLOYEE_MANAGE)

_COLUMN = {"EMAIL": "personal_email", "MOBILE": "mobile"}
_COLUMNS = (
    "c.change_id, c.employee_id, e.employee_code, e.full_name, c.field, c.old_value, c.new_value,"
    " c.status, c.requested_by, c.requested_at, c.decided_by, c.decided_at, c.decision_note,"
    " c.login_outcome"
)
_FROM = "FROM hr.employee_contact_change c JOIN hr.employee e ON e.employee_id = c.employee_id"


class ContactChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    field: Literal["EMAIL", "MOBILE"]
    new_value: str = Field(min_length=1, max_length=320)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    note: str | None = Field(default=None, max_length=500)


def _view(row: Any) -> dict[str, Any]:
    return {
        "changeId": str(row["change_id"]),
        "employeeId": str(row["employee_id"]),
        "employeeCode": row["employee_code"],
        "employeeName": row["full_name"],
        "field": row["field"],
        "oldValue": row["old_value"],
        "newValue": row["new_value"],
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


def _clean(field: str, value: str) -> str:
    try:
        return v.clean_email(value) if field == "EMAIL" else v.clean_indian_mobile(value)
    except ValueError as exc:
        raise ApiError(422, "CONTACT_NOT_VALID", str(exc)) from exc


# ---- the employee ----------------------------------------------------------------------------


@router.post("/me/employee/contact-change", status_code=201)
def request_contact_change(
    body: ContactChangeIn,
    request: Request,
    user: HumanPrincipal = Depends(current_user),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """The employee asks HR to change their email or mobile. Nothing changes until HR approves."""
    employee_id = _own_employee_id(conn, user)
    new_value = _clean(body.field, body.new_value)
    current = _fetch(conn, employee_id)[_COLUMN[body.field]]
    if (current or "").lower() == new_value.lower():
        raise conflict("CONTACT_UNCHANGED", "This is already your current value.")
    taken = (
        _email_in_use(conn, new_value, except_id=employee_id)
        if body.field == "EMAIL"
        else _mobile_in_use(conn, new_value, except_id=employee_id)
    )
    if taken:
        raise conflict("CONTACT_IN_USE", "This is already in use. Check it, or ask HR for help.")
    try:
        change_id = str(
            conn.execute(
                text(
                    "INSERT INTO hr.employee_contact_change"
                    " (employee_id, field, old_value, new_value, requested_by)"
                    " VALUES (CAST(:e AS uuid), :f, :o, :n, :u) RETURNING change_id"
                ),
                {
                    "e": employee_id,
                    "f": body.field,
                    "o": current,
                    "n": new_value,
                    "u": user.user_id,
                },
            ).scalar_one()
        )
    except IntegrityError as exc:
        conn.rollback()
        raise conflict(
            "CONTACT_CHANGE_PENDING", "You already have a request of this kind waiting for HR."
        ) from exc
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="CONTACT_CHANGE_REQUESTED",
        entity_type="employee",
        entity_id=employee_id,
        changes={"field": body.field, "from": current, "to": new_value},
        request=request,
    )
    return _view(_one(conn, change_id))


@router.get("/me/employee/contact-changes")
def my_contact_changes(
    user: HumanPrincipal = Depends(current_user),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    rows = (
        conn.execute(
            text(
                f"SELECT {_COLUMNS} {_FROM} WHERE c.employee_id = CAST(:e AS uuid)"
                " ORDER BY c.requested_at DESC LIMIT 20"
            ),
            {"e": employee_id},
        )
        .mappings()
        .all()
    )
    return {"items": [_view(r) for r in rows]}


@router.post("/me/employee/contact-changes/{change_id}/cancel")
def cancel_my_contact_change(
    change_id: str,
    request: Request,
    user: HumanPrincipal = Depends(current_user),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    change_id = _uuid(change_id)
    row = _locked_open(conn, change_id)
    if str(row["employee_id"]) != employee_id:
        raise not_found("This request was not found.")
    conn.execute(
        text(
            "UPDATE hr.employee_contact_change SET status = 'CANCELLED', decided_by = :u,"
            " decided_at = now() WHERE change_id = CAST(:id AS uuid)"
        ),
        {"u": user.user_id, "id": change_id},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="CONTACT_CHANGE_CANCELLED",
        entity_type="employee",
        entity_id=employee_id,
        changes={"field": row["field"]},
        request=request,
    )
    return _view(_one(conn, change_id))


# ---- HR --------------------------------------------------------------------------------------


@router.get("/employee-contact-changes")
def list_contact_changes(
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


def _locked_open(conn: Connection, change_id: str) -> Any:
    row = (
        conn.execute(
            text(
                "SELECT change_id, employee_id, field, old_value, new_value, status, requested_by"
                " FROM hr.employee_contact_change WHERE change_id = CAST(:id AS uuid) FOR UPDATE"
            ),
            {"id": change_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("This request was not found.")
    if row["status"] != "PENDING":
        raise conflict("CONTACT_CHANGE_DECIDED", "This request has already been decided.")
    return row


@router.post("/employee-contact-changes/{change_id}/approve")
def approve_contact_change(
    change_id: str,
    request: Request,
    body: DecisionIn | None = None,
    user: HumanPrincipal = Depends(can_manage),
    provisioner: UserProvisioner | None = Depends(get_provisioner),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """HR approves: the Verigence login is changed first (same user, same password), then HR's record.
    If the login cannot be changed nothing changes and the request stays open to try again."""
    change_id = _uuid(change_id)
    row = _locked_open(conn, change_id)
    if row["requested_by"] == user.user_id:
        raise forbidden("You cannot approve your own request. Another HR person must do it.")
    employee_id = str(row["employee_id"])
    column = _COLUMN[row["field"]]
    before = _fetch(conn, employee_id)
    if (before[column] or "") != (row["old_value"] or ""):
        raise conflict(
            "CONTACT_CHANGED_MEANWHILE",
            "The value on record is no longer what it was when this was asked. "
            "Reject this request and ask the employee to send it again.",
        )
    linked = conn.execute(
        text(
            "SELECT security_user_id IS NOT NULL FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"
        ),
        {"e": employee_id},
    ).scalar_one()
    updates: dict[str, Any] = {column: row["new_value"]}
    login_changed = _change_login_contact(
        conn,
        employee_id=employee_id,
        before=before,
        column_updates=updates,
        actor=user.user_id,
        provisioner=provisioner,
        request=request,
    )
    sets = ", ".join(f"{c} = :{c}" for c in updates)
    try:
        conn.execute(
            text(
                f"UPDATE hr.employee SET {sets}, updated_at = now(), updated_by = :actor"
                " WHERE employee_id = CAST(:id AS uuid)"
            ),
            {**updates, "actor": user.user_id, "id": employee_id},
        )
    except IntegrityError as exc:
        conn.rollback()
        raise conflict(
            "CONTACT_IN_USE", "This email or mobile number already belongs to another employee."
        ) from exc
    outcome = "LOGIN_UPDATED" if login_changed else ("LOGIN_NOT_UPDATED" if linked else "NO_LOGIN")
    decided = conn.execute(
        text(
            "UPDATE hr.employee_contact_change SET status = 'APPROVED', decided_by = :u,"
            " decided_at = now(), decision_note = :n, login_outcome = :o"
            " WHERE change_id = CAST(:id AS uuid) AND status = 'PENDING' RETURNING 1"
        ),
        {
            "u": user.user_id,
            "n": v.clean_text(body.note) if body else None,
            "o": outcome,
            "id": change_id,
        },
    ).first()
    if decided is None:
        conn.rollback()
        raise conflict("CONTACT_CHANGE_DECIDED", "This request has already been decided.")
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="CONTACT_CHANGE_APPROVED",
        entity_type="employee",
        entity_id=employee_id,
        changes={"field": row["field"], "from": row["old_value"], "to": row["new_value"]},
        request=request,
    )
    return _view(_one(conn, change_id))


@router.post("/employee-contact-changes/{change_id}/reject")
def reject_contact_change(
    change_id: str,
    body: DecisionIn,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    change_id = _uuid(change_id)
    note = v.clean_text(body.note)
    if not note or len(note) < 3:
        raise ApiError(422, "CONTACT_NOTE_REQUIRED", "Say why the request is rejected.")
    row = _locked_open(conn, change_id)
    conn.execute(
        text(
            "UPDATE hr.employee_contact_change SET status = 'REJECTED', decided_by = :u,"
            " decided_at = now(), decision_note = :n WHERE change_id = CAST(:id AS uuid)"
        ),
        {"u": user.user_id, "n": note, "id": change_id},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="CONTACT_CHANGE_REJECTED",
        entity_type="employee",
        entity_id=str(row["employee_id"]),
        changes={
            "field": row["field"],
            "from": row["old_value"],
            "to": row["new_value"],
            "note": note,
        },
        request=request,
    )
    return _view(_one(conn, change_id))
