from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from hrmgmt import permissions as perm
from hrmgmt import validators as v
from hrmgmt.audit import record_audit
from hrmgmt.db import get_conn
from hrmgmt.errors import conflict, not_found
from hrmgmt.passwords import generate_initial_password
from hrmgmt.principal import current_user, require_permission
from hrmgmt.provisioning import ProvisioningError, UserProvisioner
from hrmgmt.security import HumanPrincipal

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/hr/v1", tags=["Employees"])

can_read = require_permission(perm.HR_EMPLOYEE_READ)
can_manage = require_permission(perm.HR_EMPLOYEE_MANAGE)
can_read_sensitive = require_permission(perm.HR_SENSITIVE_READ)

Gender = Literal["MALE", "FEMALE", "OTHER"]
EmploymentStatus = Literal["ACTIVE", "INACTIVE", "EXITED"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EmployeeCreate(_Strict):
    employee_code: str = Field(min_length=2, max_length=20, pattern=r"^[A-Za-z0-9-]+$")
    full_name: str = Field(min_length=2, max_length=120)
    personal_email: str
    mobile: str | None = Field(default=None, max_length=40)
    date_of_birth: date | None = None
    gender: Gender | None = None
    qualification: str | None = Field(default=None, max_length=120)
    department: str | None = Field(default=None, max_length=60)
    address: str | None = Field(default=None, max_length=500)
    date_of_joining: date | None = None
    pan: str | None = None
    aadhaar: str | None = None
    # Default on: creating an employee also creates their Verigence login.
    create_login: bool = True

    _email = field_validator("personal_email")(lambda cls, x: v.clean_email(x))
    _pan = field_validator("pan")(lambda cls, x: v.clean_pan(x) if x else None)
    _aadhaar = field_validator("aadhaar")(lambda cls, x: v.clean_aadhaar(x) if x else None)

    @field_validator("mobile")
    @classmethod
    def _mobile(cls, x: str | None) -> str | None:
        return v.clean_indian_mobile(x) if x else None


class EmployeeUpdate(_Strict):
    """What HR may change. Every field optional; only the ones sent are changed."""

    full_name: str | None = Field(default=None, min_length=2, max_length=120)
    personal_email: str | None = None
    mobile: str | None = Field(default=None, max_length=40)
    date_of_birth: date | None = None
    gender: Gender | None = None
    qualification: str | None = Field(default=None, max_length=120)
    department: str | None = Field(default=None, max_length=60)
    designation_code: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=500)
    date_of_joining: date | None = None
    employment_status: EmploymentStatus | None = None
    pan: str | None = None
    aadhaar: str | None = None

    @field_validator("personal_email")
    @classmethod
    def _email(cls, x: str | None) -> str | None:
        return v.clean_email(x) if x is not None else None

    @field_validator("mobile")
    @classmethod
    def _mobile(cls, x: str | None) -> str | None:
        return v.clean_indian_mobile(x) if x else None

    @field_validator("pan")
    @classmethod
    def _pan(cls, x: str | None) -> str | None:
        return v.clean_pan(x) if x else None

    @field_validator("aadhaar")
    @classmethod
    def _aadhaar(cls, x: str | None) -> str | None:
        return v.clean_aadhaar(x) if x else None


class SelfUpdate(_Strict):
    """The only things an employee may change about themselves."""

    address: str | None = Field(default=None, max_length=500)
    emergency_contact_name: str | None = Field(default=None, max_length=120)
    emergency_contact_number: str | None = Field(default=None, max_length=40)
    secondary_email: str | None = None

    @field_validator("emergency_contact_number")
    @classmethod
    def _emergency(cls, x: str | None) -> str | None:
        return v.clean_indian_mobile(x) if x else None

    @field_validator("secondary_email")
    @classmethod
    def _secondary(cls, x: str | None) -> str | None:
        return v.clean_email(x) if x else None


_PUBLIC_COLUMNS = """
    e.employee_id, e.employee_code, e.full_name, e.date_of_birth, e.gender, e.mobile,
    e.personal_email, e.secondary_email, e.qualification, e.department, e.designation_code,
    d.label AS designation, e.address, e.emergency_contact_name, e.emergency_contact_number,
    e.date_of_joining, e.employment_status, e.login_status, e.login_error_code,
    s.pan, s.aadhaar,
    (s.pan IS NOT NULL AND EXISTS (
        SELECT 1 FROM hr.employee_sensitive o
        WHERE o.pan = s.pan AND o.employee_id <> e.employee_id)) AS pan_duplicate
"""
_FROM = """
    FROM hr.employee e
    LEFT JOIN hr.employee_sensitive s ON s.employee_id = e.employee_id
    LEFT JOIN hr.designation d ON d.code = e.designation_code
"""


def _view(row: Any) -> dict[str, Any]:
    """An employee as the API shows them: protected numbers are always masked here."""
    flags = []
    if not row["pan"]:
        flags.append("PAN_MISSING")
    elif row["pan_duplicate"]:
        flags.append("PAN_DUPLICATE")
    if not row["aadhaar"]:
        flags.append("AADHAAR_MISSING")
    if not row["mobile"]:
        flags.append("MOBILE_MISSING")
    return {
        "employeeId": str(row["employee_id"]),
        "employeeCode": row["employee_code"],
        "fullName": row["full_name"],
        "dateOfBirth": row["date_of_birth"].isoformat() if row["date_of_birth"] else None,
        "gender": row["gender"],
        "mobile": row["mobile"],
        "personalEmail": row["personal_email"],
        "secondaryEmail": row["secondary_email"],
        "qualification": row["qualification"],
        "department": row["department"],
        "designationCode": row["designation_code"],
        "designation": row["designation"],
        "address": row["address"],
        "emergencyContactName": row["emergency_contact_name"],
        "emergencyContactNumber": row["emergency_contact_number"],
        "dateOfJoining": row["date_of_joining"].isoformat() if row["date_of_joining"] else None,
        "employmentStatus": row["employment_status"],
        "loginStatus": row["login_status"],
        "loginErrorCode": row["login_error_code"],
        "panMasked": v.mask_pan(row["pan"]),
        "aadhaarMasked": v.mask_aadhaar(row["aadhaar"]),
        "dataFlags": flags,
    }


def _fetch(conn: Connection, employee_id: str) -> Any:
    row = (
        conn.execute(
            text(f"SELECT {_PUBLIC_COLUMNS} {_FROM} WHERE e.employee_id = CAST(:id AS uuid)"),
            {"id": employee_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found("Employee not found.")
    return row


def get_provisioner(request: Request) -> UserProvisioner | None:
    return getattr(request.app.state, "provisioner", None)


def _create_login(
    conn: Connection,
    *,
    employee_id: str,
    actor: str,
    provisioner: UserProvisioner | None,
    request: Request,
) -> str | None:
    """Create the Verigence login for an employee. Returns the one-time initial password, or None
    when the login could not be created (the reason is stored for HR; the employee is kept)."""
    row = _fetch(conn, employee_id)
    first, last = v.split_name(row["full_name"])
    mobile = row["mobile"]
    password = generate_initial_password()
    code: str | None = None
    user_id: str | None = None
    if provisioner is None:
        code = "NOT_CONFIGURED"
    elif not mobile:
        code = "CONTACT_NOT_VALID"  # Security needs a valid Indian mobile for every user
    else:
        # The employee row is committed before this network call (no open transaction held).
        try:
            user_id = provisioner.create_user(
                first_name=first,
                last_name=last,
                email=row["personal_email"],
                mobile=mobile,
                password=password,
            ).user_id
        except ProvisioningError as exc:
            code = exc.code
    if user_id is not None:
        conn.execute(
            text(
                "UPDATE hr.employee SET security_user_id = CAST(:u AS uuid), login_status = 'CREATED',"
                " login_error_code = NULL, updated_at = now(), updated_by = :a"
                " WHERE employee_id = CAST(:id AS uuid)"
            ),
            {"u": user_id, "a": actor, "id": employee_id},
        )
        # The password itself is never recorded anywhere.
        record_audit(
            conn,
            actor_user_id=actor,
            action="LOGIN_CREATED",
            entity_type="employee",
            entity_id=employee_id,
            changes={"securityUserId": user_id, "password": "not recorded"},
            request=request,
        )
        return password
    conn.execute(
        text(
            "UPDATE hr.employee SET login_status = 'FAILED', login_error_code = :c,"
            " updated_at = now(), updated_by = :a WHERE employee_id = CAST(:id AS uuid)"
        ),
        {"c": code, "a": actor, "id": employee_id},
    )
    record_audit(
        conn,
        actor_user_id=actor,
        action="LOGIN_CREATE_FAILED",
        entity_type="employee",
        entity_id=employee_id,
        changes={"reason": code},
        request=request,
    )
    return None


@router.post("/employees", status_code=201)
def create_employee(
    body: EmployeeCreate,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    provisioner: UserProvisioner | None = Depends(get_provisioner),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Create the employee record and, by default, their Verigence login. A login problem never
    loses the employee: the record is kept and HR sees why the login is pending."""
    try:
        employee_id = str(
            conn.execute(
                text(
                    """
                    INSERT INTO hr.employee
                        (employee_code, full_name, date_of_birth, gender, mobile, personal_email,
                         qualification, department, address, date_of_joining, created_by, updated_by)
                    VALUES (:code, :name, :dob, :gender, :mobile, :email, :qual, :dept, :addr,
                            :doj, :actor, :actor)
                    RETURNING employee_id
                    """
                ),
                {
                    "code": body.employee_code.upper(),
                    "name": v.clean_text(body.full_name),
                    "dob": body.date_of_birth,
                    "gender": body.gender,
                    "mobile": body.mobile,
                    "email": body.personal_email,
                    "qual": v.clean_text(body.qualification),
                    "dept": v.clean_text(body.department),
                    "addr": v.clean_text(body.address),
                    "doj": body.date_of_joining,
                    "actor": user.user_id,
                },
            ).scalar_one()
        )
        if body.pan or body.aadhaar:
            conn.execute(
                text(
                    "INSERT INTO hr.employee_sensitive (employee_id, pan, aadhaar, updated_by)"
                    " VALUES (CAST(:id AS uuid), :pan, :aadhaar, :actor)"
                ),
                {
                    "id": employee_id,
                    "pan": body.pan,
                    "aadhaar": body.aadhaar,
                    "actor": user.user_id,
                },
            )
    except IntegrityError as exc:
        conn.rollback()
        text_error = str(exc.orig)
        if "employee_code_uq" in text_error:
            raise conflict("EMPLOYEE_CODE_EXISTS", "This employee code is already in use.") from exc
        if "employee_email_uq" in text_error:
            raise conflict(
                "EMPLOYEE_EMAIL_EXISTS", "This email already belongs to an employee."
            ) from exc
        raise
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="EMPLOYEE_CREATED",
        entity_type="employee",
        entity_id=employee_id,
        changes={
            "employeeCode": body.employee_code.upper(),
            "pan": "set" if body.pan else "not provided",
            "aadhaar": "set" if body.aadhaar else "not provided",
        },
        request=request,
    )
    conn.commit()

    initial_password: str | None = None
    if body.create_login:
        initial_password = _create_login(
            conn,
            employee_id=employee_id,
            actor=user.user_id,
            provisioner=provisioner,
            request=request,
        )
    result: dict[str, Any] = {"employee": _view(_fetch(conn, employee_id))}
    if initial_password is not None:
        # Shown once, to the HR user who created the employee, so it can be handed over.
        result["initialPassword"] = initial_password
        result["initialPasswordNote"] = "Shown once. It is not stored. Share it securely."
    return result


@router.post("/employees/{employee_id}/login")
def create_login(
    employee_id: str,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    provisioner: UserProvisioner | None = Depends(get_provisioner),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Retry login creation for an employee whose login is not yet created (HR presses it)."""
    row = _fetch(conn, employee_id)
    if row["login_status"] == "CREATED":
        raise conflict("LOGIN_ALREADY_CREATED", "This employee already has a Verigence login.")
    password = _create_login(
        conn, employee_id=employee_id, actor=user.user_id, provisioner=provisioner, request=request
    )
    result: dict[str, Any] = {"employee": _view(_fetch(conn, employee_id))}
    if password is not None:
        result["initialPassword"] = password
        result["initialPasswordNote"] = "Shown once. It is not stored. Share it securely."
    return result


@router.get("/employees")
def list_employees(
    q: Annotated[str | None, Query(max_length=80)] = None,
    status: Annotated[EmploymentStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    _: HumanPrincipal = Depends(can_read),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    pattern = f"%{q.strip()}%" if q and q.strip() else None
    where = """
        WHERE (CAST(:status AS text) IS NULL OR e.employment_status = :status)
          AND (CAST(:pattern AS text) IS NULL OR e.full_name ILIKE :pattern
               OR e.employee_code ILIKE :pattern OR e.personal_email ILIKE :pattern)
    """
    params = {"status": status, "pattern": pattern, "limit": limit, "offset": offset}
    total = conn.execute(text(f"SELECT count(*) {_FROM} {where}"), params).scalar_one()
    rows = (
        conn.execute(
            text(
                f"SELECT {_PUBLIC_COLUMNS} {_FROM} {where}"
                " ORDER BY e.employee_code LIMIT :limit OFFSET :offset"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return {"total": total, "items": [_view(r) for r in rows]}


@router.get("/employees/{employee_id}")
def get_employee(
    employee_id: str,
    _: HumanPrincipal = Depends(can_read),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    return _view(_fetch(conn, _uuid(employee_id)))


@router.patch("/employees/{employee_id}")
def update_employee(
    employee_id: str,
    body: EmployeeUpdate,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _uuid(employee_id)
    before = _fetch(conn, employee_id)
    sent = body.model_dump(exclude_unset=True)
    changes: dict[str, Any] = {}
    column_updates: dict[str, Any] = {}
    sensitive_updates: dict[str, Any] = {}
    for field, value in sent.items():
        if field in ("pan", "aadhaar"):
            sensitive_updates[field] = value
            changes[field] = "changed"  # protected: never record the value
            continue
        if field == "designation_code" and value is not None:
            known = conn.execute(
                text("SELECT 1 FROM hr.designation WHERE code = :c AND active"), {"c": value}
            ).first()
            if known is None:
                raise conflict("DESIGNATION_UNKNOWN", "Choose one of the listed designations.")
        if field in ("full_name", "qualification", "department", "address"):
            value = v.clean_text(value)
        old = before[field] if field in before else None
        old_out = old.isoformat() if isinstance(old, date) else old
        new_out = value.isoformat() if isinstance(value, date) else value
        if old_out != new_out:
            column_updates[field] = value
            changes[field] = {"from": old_out, "to": new_out}
    if not changes:
        return _view(before)
    try:
        if column_updates:
            sets = ", ".join(f"{c} = :{c}" for c in column_updates)
            conn.execute(
                text(
                    f"UPDATE hr.employee SET {sets}, updated_at = now(), updated_by = :actor"
                    " WHERE employee_id = CAST(:id AS uuid)"
                ),
                {**column_updates, "actor": user.user_id, "id": employee_id},
            )
        if sensitive_updates:
            cols = list(sensitive_updates)
            conn.execute(
                text(
                    f"INSERT INTO hr.employee_sensitive (employee_id, {', '.join(cols)}, updated_by)"
                    f" VALUES (CAST(:id AS uuid), {', '.join(':' + c for c in cols)}, :actor)"
                    " ON CONFLICT (employee_id) DO UPDATE SET "
                    + ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
                    + ", updated_at = now(), updated_by = EXCLUDED.updated_by"
                ),
                {**sensitive_updates, "id": employee_id, "actor": user.user_id},
            )
    except IntegrityError as exc:
        conn.rollback()
        if "employee_email_uq" in str(exc.orig):
            raise conflict(
                "EMPLOYEE_EMAIL_EXISTS", "This email already belongs to an employee."
            ) from exc
        raise
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="EMPLOYEE_UPDATED",
        entity_type="employee",
        entity_id=employee_id,
        changes=changes,
        request=request,
    )
    return _view(_fetch(conn, employee_id))


@router.get("/employees/{employee_id}/sensitive")
def reveal_sensitive(
    employee_id: str,
    request: Request,
    user: HumanPrincipal = Depends(can_read_sensitive),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Full PAN and Aadhaar. Separate permission; every reveal is written to the audit log."""
    employee_id = _uuid(employee_id)
    _fetch(conn, employee_id)
    row = (
        conn.execute(
            text(
                "SELECT pan, aadhaar FROM hr.employee_sensitive WHERE employee_id = CAST(:id AS uuid)"
            ),
            {"id": employee_id},
        )
        .mappings()
        .first()
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="SENSITIVE_REVEALED",
        entity_type="employee",
        entity_id=employee_id,
        changes={"pan": "revealed", "aadhaar": "revealed"},
        request=request,
    )
    return {"pan": row["pan"] if row else None, "aadhaar": row["aadhaar"] if row else None}


# ---- Self service: an employee's own record, found by their Security login, never by an id
# the client supplies. ---------------------------------------------------------------------


def _own_employee_id(conn: Connection, user: HumanPrincipal) -> str:
    row = conn.execute(
        text(
            "SELECT employee_id FROM hr.employee"
            " WHERE security_user_id = CAST(:u AS uuid) AND employment_status = 'ACTIVE'"
        ),
        {"u": _uuid(user.user_id)},
    ).first()
    if row is None:
        raise not_found("No employee record is linked to your login.")
    return str(row[0])


@router.get("/me/employee")
def my_record(
    user: HumanPrincipal = Depends(current_user), conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    return _view(_fetch(conn, _own_employee_id(conn, user)))


@router.patch("/me/employee")
def update_my_record(
    body: SelfUpdate,
    request: Request,
    user: HumanPrincipal = Depends(current_user),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    before = _fetch(conn, employee_id)
    sent = body.model_dump(exclude_unset=True)
    changes: dict[str, Any] = {}
    updates: dict[str, Any] = {}
    for field, value in sent.items():
        if field in ("address", "emergency_contact_name"):
            value = v.clean_text(value)
        if before[field] != value:
            updates[field] = value
            changes[field] = {"from": before[field], "to": value}
    if not updates:
        return _view(before)
    sets = ", ".join(f"{c} = :{c}" for c in updates)
    conn.execute(
        text(
            f"UPDATE hr.employee SET {sets}, updated_at = now(), updated_by = :actor"
            " WHERE employee_id = CAST(:id AS uuid)"
        ),
        {**updates, "actor": user.user_id, "id": employee_id},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="EMPLOYEE_SELF_UPDATED",
        entity_type="employee",
        entity_id=employee_id,
        changes=changes,
        request=request,
    )
    return _view(_fetch(conn, employee_id))


@router.get("/me/employee/sensitive")
def reveal_my_sensitive(
    request: Request,
    user: HumanPrincipal = Depends(current_user),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    row = (
        conn.execute(
            text(
                "SELECT pan, aadhaar FROM hr.employee_sensitive WHERE employee_id = CAST(:id AS uuid)"
            ),
            {"id": employee_id},
        )
        .mappings()
        .first()
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="SENSITIVE_REVEALED_SELF",
        entity_type="employee",
        entity_id=employee_id,
        changes={"pan": "revealed", "aadhaar": "revealed"},
        request=request,
    )
    return {"pan": row["pan"] if row else None, "aadhaar": row["aadhaar"] if row else None}


def _uuid(value: str) -> str:
    import uuid

    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise not_found("Not found.") from exc
