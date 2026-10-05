from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from hrmgmt import permissions as perm
from hrmgmt.api.employees import _create_login, _uuid, get_provisioner
from hrmgmt.audit import record_audit
from hrmgmt.db import get_conn
from hrmgmt.errors import ApiError, dependency_unavailable
from hrmgmt.principal import require_permission
from hrmgmt.provisioning import ProvisioningError, SyncOutcome, UserProvisioner, UserSummary
from hrmgmt.security import HumanPrincipal

router = APIRouter(prefix="/hr/v1", tags=["Employee sync"])

can_manage = require_permission(perm.HR_EMPLOYEE_MANAGE)

PAGE = 200
MAX_USERS = 5000
SECURITY_BATCH = 100
# Logins are created in groups of five, one request per group, so no request runs long.
CREATE_GROUP = 5
# Answers from Security that a second try cannot fix: HR has to correct the employee's details first.
_NEEDS_FIXING = ("EMAIL_OR_MOBILE_EXISTS", "CONTACT_NOT_VALID", "NOT_PERMITTED")


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
                " employment_status, security_user_id, mobile, login_error_code"
                " FROM hr.employee ORDER BY employee_code"
            )
        )
        .mappings()
        .all()
    )
    linked_ids = {str(e["security_user_id"]) for e in employees if e["security_user_id"]}
    mobile_count: dict[str, int] = {}
    for e in employees:
        if e["mobile"]:
            mobile_count[e["mobile"]] = mobile_count.get(e["mobile"], 0) + 1
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
                blocked = _cannot_create(e, mobile_count)
                unmatched.append(
                    {
                        "employeeId": eid,
                        "code": code,
                        "name": name,
                        "reason": "NO_LOGIN",
                        "email": e["personal_email"],
                        "canCreate": blocked is None,
                        "blocked": blocked,
                    }
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
        known_emails = {
            (e["personal_email"] or "").strip().lower(),
            (e["secondary_email"] or "").strip().lower(),
        }
        email_differs = bool(user.email) and user.email.strip().lower() not in known_emails
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
                "userEmail": user.email,
                "emailDiffers": email_differs,
            }
        )
    return items, unmatched


def _cannot_create(e: Any, mobile_count: dict[str, int]) -> str | None:
    """Why a login cannot be created for this employee now, or None when it can."""
    if e["employment_status"] != "ACTIVE":
        return "EMPLOYEE_NOT_ACTIVE"
    if not e["mobile"]:
        return "NO_MOBILE"
    if mobile_count.get(e["mobile"], 0) > 1:
        return "MOBILE_SHARED"
    if e["login_error_code"] in _NEEDS_FIXING:
        return str(e["login_error_code"])
    return None


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
        "emailDiffers": sum(1 for i in items if i["emailDiffers"]),
        "toCreate": sum(1 for u in unmatched if u.get("canCreate")),
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
    shown = [
        i
        for i in items
        if i["link"] or i["tick"] or i["suspend"] or i["attention"] or i["emailDiffers"]
    ]
    return {"applied": body.apply, "summary": summary, "items": shown, "unmatched": unmatched}


class CreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_ids: list[str] = Field(min_length=1, max_length=CREATE_GROUP, alias="employeeIds")


@router.post("/employees/sync-users/create")
def create_missing_logins(
    body: CreateIn,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    provisioner: UserProvisioner | None = Depends(get_provisioner),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Creates the Verigence login (ACTIVE, no OTP step) for up to five employees who have none.
    Each employee is checked again just before: still active, still without a login, has a mobile
    that no other employee uses, and no Verigence user already has the email (that one is linked
    by the sync instead). One attempt each, never retried here. The temporary password is random,
    never shown and never stored: the Welcome email sets the one the employee is given."""
    if provisioner is None:
        raise dependency_unavailable("The login service is not configured.")
    ids = list(dict.fromkeys(body.employee_ids))
    results: list[dict[str, Any]] = []
    for employee_id in ids:
        results.append(_create_one(conn, employee_id, user.user_id, provisioner, request))
        conn.commit()  # each login is saved at once, so a later problem never loses an earlier one
    return {"results": results}


def _create_one(
    conn: Connection,
    employee_id: str,
    actor: str,
    provisioner: UserProvisioner,
    request: Request,
) -> dict[str, Any]:
    row = None
    try:
        known = _uuid(employee_id)
    except ApiError:
        known = None
    if known is not None:
        row = (
            conn.execute(
                text(
                    "SELECT employee_code, full_name, personal_email, employment_status, mobile,"
                    " security_user_id,"
                    " (SELECT count(*) FROM hr.employee o WHERE o.mobile = e.mobile"
                    "   AND o.employee_id <> e.employee_id) AS mobile_others"
                    " FROM hr.employee e WHERE employee_id = CAST(:id AS uuid)"
                ),
                {"id": known},
            )
            .mappings()
            .first()
        )

    def result(outcome: str, reason: str | None = None) -> dict[str, Any]:
        return {
            "employeeId": employee_id,
            "code": row["employee_code"] if row else None,
            "name": row["full_name"] if row else None,
            "outcome": outcome,
            "reason": reason,
        }

    if row is None:
        return result("SKIPPED", "NOT_FOUND")
    if row["security_user_id"]:
        return result("SKIPPED", "ALREADY_LINKED")
    if row["employment_status"] != "ACTIVE":
        return result("SKIPPED", "EMPLOYEE_NOT_ACTIVE")
    if not row["mobile"]:
        return result("SKIPPED", "NO_MOBILE")
    if row["mobile_others"]:
        return result("SKIPPED", "MOBILE_SHARED")
    conn.rollback()  # nothing is held open while Security is asked
    try:
        existing = provisioner.find_user(email=row["personal_email"])
    except ProvisioningError as exc:
        return result("FAILED", exc.code)
    if existing is not None:
        return result("SKIPPED", "LOGIN_EXISTS")  # the sync links it; nothing is created twice
    if (
        _create_login(
            conn, employee_id=employee_id, actor=actor, provisioner=provisioner, request=request
        )
        is not None
    ):
        return result("CREATED")
    code = conn.execute(
        text("SELECT login_error_code FROM hr.employee WHERE employee_id = CAST(:id AS uuid)"),
        {"id": employee_id},
    ).scalar_one()
    return result("FAILED", code)
