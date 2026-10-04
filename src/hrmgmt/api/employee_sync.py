from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, text

from hrmgmt import permissions as perm
from hrmgmt.api.employees import get_provisioner
from hrmgmt.audit import record_audit
from hrmgmt.db import get_conn
from hrmgmt.errors import dependency_unavailable
from hrmgmt.principal import require_permission
from hrmgmt.provisioning import ProvisioningError, SyncOutcome, UserProvisioner, UserSummary
from hrmgmt.security import HumanPrincipal

router = APIRouter(prefix="/hr/v1", tags=["Employee sync"])

can_manage = require_permission(perm.HR_EMPLOYEE_MANAGE)

PAGE = 200
MAX_USERS = 5000
SECURITY_BATCH = 100


class SyncIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # False (the default) only reports what would change. True makes the changes.
    apply: bool = False


def _all_users(provisioner: UserProvisioner) -> list[UserSummary]:
    users: list[UserSummary] = []
    offset = 0
    while offset < MAX_USERS:
        page = provisioner.list_users(limit=PAGE, offset=offset)
        users.extend(page)
        if len(page) < PAGE:
            break
        offset += PAGE
    return users


def _plan(
    conn: Connection, users: list[UserSummary]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_id = {u.user_id: u for u in users}
    by_email = {u.email.strip().lower(): u for u in users if u.email}
    employees = (
        conn.execute(
            text(
                "SELECT employee_id, employee_code, full_name, personal_email, secondary_email,"
                " employment_status, security_user_id FROM hr.employee ORDER BY employee_code"
            )
        )
        .mappings()
        .all()
    )
    linked_ids = {str(e["security_user_id"]) for e in employees if e["security_user_id"]}
    claimed: set[str] = set()
    items: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for e in employees:
        eid, code, name = str(e["employee_id"]), e["employee_code"], e["full_name"]
        user: UserSummary | None = None
        link = False
        if e["security_user_id"]:
            user = by_id.get(str(e["security_user_id"]))
            if user is None:
                unmatched.append(
                    {"employeeId": eid, "code": code, "name": name, "reason": "LINKED_USER_MISSING"}
                )
                continue
        else:
            for email in (e["personal_email"], e["secondary_email"]):
                candidate = by_email.get((email or "").strip().lower())
                if candidate is not None:
                    user = candidate
                    break
            if user is None:
                unmatched.append(
                    {"employeeId": eid, "code": code, "name": name, "reason": "NO_LOGIN"}
                )
                continue
            if user.user_id in linked_ids or user.user_id in claimed:
                unmatched.append(
                    {"employeeId": eid, "code": code, "name": name, "reason": "LOGIN_IN_USE"}
                )
                continue
            link = True
        claimed.add(user.user_id)
        active = e["employment_status"] == "ACTIVE"
        suspend = (not active) and user.status == "ACTIVE"
        attention: list[str] = []
        if active and user.status == "SUSPENDED":
            attention.append("EMPLOYEE_ACTIVE_USER_SUSPENDED")
        if active and user.status == "PENDING":
            attention.append("USER_PENDING_APPROVAL")
        if (not active) and user.status == "PENDING":
            attention.append("EMPLOYEE_NOT_ACTIVE_USER_PENDING")
        items.append(
            {
                "employeeId": eid,
                "code": code,
                "name": name,
                "employmentStatus": e["employment_status"],
                "userId": user.user_id,
                "userName": user.display_name,
                "userStatus": user.status,
                "link": link,
                "tick": not user.is_employee,
                "suspend": suspend,
                "attention": attention,
            }
        )
    return items, unmatched


@router.post("/employees/sync-users")
def sync_users(
    body: SyncIn,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    provisioner: UserProvisioner | None = Depends(get_provisioner),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Compares HR's employees with the Verigence users. A user whose email matches an employee
    (or who is already linked) gets Is Employee ticked and is linked to the employee; when the
    employee is not active the ACTIVE user is suspended. Users are never reactivated here. Without
    `apply` it only reports; with it, it makes the changes."""
    if provisioner is None:
        raise dependency_unavailable("The login service is not configured.")
    try:
        users = _all_users(provisioner)
    except ProvisioningError as exc:
        raise dependency_unavailable(
            f"The user list could not be loaded ({exc.code}). Please try again."
        ) from exc
    items, unmatched = _plan(conn, users)
    actionable = [i for i in items if i["tick"] or i["suspend"]]
    summary = {
        "employees": len(items) + len(unmatched),
        "matched": len(items),
        "toLink": sum(1 for i in items if i["link"]),
        "toTick": sum(1 for i in items if i["tick"]),
        "toSuspend": sum(1 for i in items if i["suspend"]),
        "unmatched": len(unmatched),
        "needAttention": sum(1 for i in items if i["attention"]),
    }
    results: dict[str, SyncOutcome] = {}
    if body.apply:
        try:
            for start in range(0, len(actionable), SECURITY_BATCH):
                chunk = actionable[start : start + SECURITY_BATCH]
                outcomes = provisioner.sync_employees(
                    items=[(i["userId"], i["suspend"]) for i in chunk]
                )
                results.update({o.user_id: o for o in outcomes})
        except ProvisioningError as exc:
            # What Security already changed stays changed; running the sync again continues it.
            raise dependency_unavailable(
                f"Security could not apply the changes ({exc.code}). Run the sync again."
            ) from exc
        for i in items:
            if i["link"]:
                conn.execute(
                    text(
                        "UPDATE hr.employee SET security_user_id = CAST(:u AS uuid),"
                        " login_status = 'CREATED', login_error_code = NULL, updated_at = now(),"
                        " updated_by = :a WHERE employee_id = CAST(:e AS uuid)"
                        " AND security_user_id IS NULL"
                    ),
                    {"u": i["userId"], "a": user.user_id, "e": i["employeeId"]},
                )
            outcome = results.get(i["userId"])
            if i["link"] or (outcome and (outcome.ticked or outcome.suspended)):
                record_audit(
                    conn,
                    actor_user_id=user.user_id,
                    action="EMPLOYEE_USER_SYNCED",
                    entity_type="employee",
                    entity_id=i["employeeId"],
                    changes={
                        "linked": i["link"],
                        "isEmployeeTicked": bool(outcome and outcome.ticked),
                        "userSuspended": bool(outcome and outcome.suspended),
                    },
                    request=request,
                )
        for i in items:
            outcome = results.get(i["userId"])
            i["done"] = {
                "linked": i["link"],
                "ticked": bool(outcome and outcome.ticked),
                "suspended": bool(outcome and outcome.suspended),
                "note": outcome.note if outcome else None,
            }
    shown = [i for i in items if i["link"] or i["tick"] or i["suspend"] or i["attention"]]
    return {"applied": body.apply, "summary": summary, "items": shown, "unmatched": unmatched}
