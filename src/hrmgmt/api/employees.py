from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from hrmgmt import permissions as perm
from hrmgmt import validators as v
from hrmgmt.audit import record_audit
from hrmgmt.catalog import STATES, canonical_state
from hrmgmt.db import get_conn
from hrmgmt.errors import ApiError, conflict, dependency_unavailable, not_found
from hrmgmt.passwords import generate_initial_password
from hrmgmt.photos import MAX_UPLOAD_BYTES, PhotoError, normalise_profile_photo
from hrmgmt.principal import current_user, require_permission
from hrmgmt.provisioning import ProvisioningError, UserProvisioner
from hrmgmt.security import HumanPrincipal
from hrmgmt.storage import ObjectStorage, StorageError

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/hr/v1", tags=["Employees"])

can_read = require_permission(perm.HR_EMPLOYEE_READ)
can_manage = require_permission(perm.HR_EMPLOYEE_MANAGE)
can_read_sensitive = require_permission(perm.HR_SENSITIVE_READ)

Gender = Literal["MALE", "FEMALE", "OTHER"]
EmploymentStatus = Literal["ACTIVE", "INACTIVE", "EXITED"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class QualificationIn(_Strict):
    """One qualification: the degree (from the catalogue), marks as a percentage, year passed."""

    degree_code: str = Field(min_length=2, max_length=20)
    degree_other: str | None = Field(default=None, max_length=120)
    percentage: Decimal = Field(ge=0, le=100, decimal_places=2)
    year_of_passing: int = Field(ge=1950, le=2100)

    @model_validator(mode="after")
    def _other_needs_a_name(self) -> QualificationIn:
        if self.year_of_passing > date.today().year:
            raise ValueError("Year of passing cannot be in the future")
        if self.degree_code == "OTHER":
            if not v.clean_text(self.degree_other):
                raise ValueError("Type the name of the degree")
            self.degree_other = v.clean_text(self.degree_other)
        else:
            self.degree_other = None
        return self


def _state(x: str | None) -> str | None:
    return canonical_state(x) if x else None


def _pincode(x: str | None) -> str | None:
    if not x:
        return None
    digits = x.strip()
    if len(digits) != 6 or not digits.isdigit() or digits[0] == "0":
        raise ValueError("Pincode must be 6 digits and cannot start with 0")
    return digits


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
    state: str | None = Field(default=None, max_length=60)
    pincode: str | None = Field(default=None, max_length=10)
    total_experience_years: Decimal | None = Field(default=None, ge=0, le=60, decimal_places=1)
    emergency_contact_name: str | None = Field(default=None, max_length=120)
    emergency_contact_number: str | None = Field(default=None, max_length=40)
    emergency_contact_address: str | None = Field(default=None, max_length=500)
    date_of_joining: date | None = None
    pan: str | None = None
    aadhaar: str | None = None
    qualifications: list[QualificationIn] = Field(default_factory=list, max_length=10)
    # Default on: creating an employee also creates their Verigence login.
    create_login: bool = True

    _email = field_validator("personal_email")(lambda cls, x: v.clean_email(x))
    _pan = field_validator("pan")(lambda cls, x: v.clean_pan(x) if x else None)
    _aadhaar = field_validator("aadhaar")(lambda cls, x: v.clean_aadhaar(x) if x else None)
    _state = field_validator("state")(lambda cls, x: _state(x))
    _pincode = field_validator("pincode")(lambda cls, x: _pincode(x))
    _emergency = field_validator("emergency_contact_number")(
        lambda cls, x: v.clean_indian_mobile(x) if x else None
    )

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
    state: str | None = Field(default=None, max_length=60)
    pincode: str | None = Field(default=None, max_length=10)
    total_experience_years: Decimal | None = Field(default=None, ge=0, le=60, decimal_places=1)
    emergency_contact_name: str | None = Field(default=None, max_length=120)
    emergency_contact_number: str | None = Field(default=None, max_length=40)
    emergency_contact_address: str | None = Field(default=None, max_length=500)
    date_of_joining: date | None = None
    employment_status: EmploymentStatus | None = None
    pan: str | None = None
    aadhaar: str | None = None

    _state = field_validator("state")(lambda cls, x: _state(x))
    _pincode = field_validator("pincode")(lambda cls, x: _pincode(x))
    _emergency = field_validator("emergency_contact_number")(
        lambda cls, x: v.clean_indian_mobile(x) if x else None
    )

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
    state: str | None = Field(default=None, max_length=60)
    pincode: str | None = Field(default=None, max_length=10)
    emergency_contact_name: str | None = Field(default=None, max_length=120)
    emergency_contact_number: str | None = Field(default=None, max_length=40)
    emergency_contact_address: str | None = Field(default=None, max_length=500)
    secondary_email: str | None = None

    _state = field_validator("state")(lambda cls, x: _state(x))
    _pincode = field_validator("pincode")(lambda cls, x: _pincode(x))

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
    e.personal_email, e.secondary_email, e.qualification, e.state, e.pincode,
    e.total_experience_years, e.emergency_contact_address, e.photo_updated_at, e.department, e.designation_code,
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
        "state": row["state"],
        "pincode": row["pincode"],
        "totalExperienceYears": (
            float(row["total_experience_years"])
            if row["total_experience_years"] is not None
            else None
        ),
        "emergencyContactAddress": row["emergency_contact_address"],
        "hasPhoto": row["photo_updated_at"] is not None,
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


def create_employee_record(
    conn: Connection,
    *,
    body: EmployeeCreate,
    actor: str,
    provisioner: UserProvisioner | None,
    request: Request,
) -> dict[str, Any]:
    """Create one employee and, when asked, their Verigence login. A login problem never loses
    the employee: the record is kept and HR sees why the login is pending. Shared by the single
    "Add employee" call and the spreadsheet import so both behave identically."""
    try:
        employee_id = str(
            conn.execute(
                text(
                    """
                    INSERT INTO hr.employee
                        (employee_code, full_name, date_of_birth, gender, mobile, personal_email,
                         qualification, department, address, state, pincode,
                         total_experience_years, emergency_contact_name, emergency_contact_number,
                         emergency_contact_address, date_of_joining, created_by, updated_by)
                    VALUES (:code, :name, :dob, :gender, :mobile, :email, :qual, :dept, :addr,
                            :state, :pincode, :exp, :ec_name, :ec_number, :ec_addr,
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
                    "state": body.state,
                    "pincode": body.pincode,
                    "exp": body.total_experience_years,
                    "ec_name": v.clean_text(body.emergency_contact_name),
                    "ec_number": body.emergency_contact_number,
                    "ec_addr": v.clean_text(body.emergency_contact_address),
                    "doj": body.date_of_joining,
                    "actor": actor,
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
                    "actor": actor,
                },
            )
        for q in body.qualifications:
            _insert_qualification(conn, employee_id, q, actor)
    except UnknownDegree as exc:
        conn.rollback()
        raise conflict("DEGREE_UNKNOWN", "Choose a degree from the list.") from exc
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
        actor_user_id=actor,
        action="EMPLOYEE_CREATED",
        entity_type="employee",
        entity_id=employee_id,
        changes={
            "employeeCode": body.employee_code.upper(),
            "pan": "set" if body.pan else "not provided",
            "aadhaar": "set" if body.aadhaar else "not provided",
            "qualifications": len(body.qualifications),
        },
        request=request,
    )
    conn.commit()

    initial_password: str | None = None
    if body.create_login:
        initial_password = _create_login(
            conn,
            employee_id=employee_id,
            actor=actor,
            provisioner=provisioner,
            request=request,
        )
    result: dict[str, Any] = {"employee": _detail(conn, employee_id)}
    if initial_password is not None:
        # Shown once, to the HR user who created the employee, so it can be handed over.
        result["initialPassword"] = initial_password
        result["initialPasswordNote"] = "Shown once. It is not stored. Share it securely."
    return result


@router.post("/employees", status_code=201)
def create_employee(
    body: EmployeeCreate,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    provisioner: UserProvisioner | None = Depends(get_provisioner),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    return create_employee_record(
        conn, body=body, actor=user.user_id, provisioner=provisioner, request=request
    )


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


class LinkLogin(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str | None = Field(default=None, max_length=320)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return v.clean_email(value) if value else None


@router.post("/employees/{employee_id}/link-login")
def link_login(
    employee_id: str,
    body: LinkLogin,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    provisioner: UserProvisioner | None = Depends(get_provisioner),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Link an employee to a Verigence login that already exists (for example a person who had a
    login before HR). Matches by email: the employee's own, or the one HR types."""
    row = _fetch(conn, employee_id)
    linked = conn.execute(
        text("SELECT security_user_id FROM hr.employee WHERE employee_id = CAST(:id AS uuid)"),
        {"id": employee_id},
    ).scalar_one()
    if linked is not None:
        raise conflict("LOGIN_ALREADY_LINKED", "This employee is already linked to a login.")
    if provisioner is None:
        raise dependency_unavailable("Verigence login service is not configured.")
    email = body.email or row["personal_email"]
    try:
        found = provisioner.find_user(email=email)
    except ProvisioningError as exc:
        raise dependency_unavailable(
            f"The Verigence user could not be looked up ({exc.code}). Please try again."
        ) from exc
    if found is None:
        raise ApiError(404, "LOGIN_NOT_FOUND", "No Verigence user has this email.")
    if found.status != "ACTIVE":
        raise conflict("LOGIN_NOT_ACTIVE", "That Verigence user is not active.")
    taken = conn.execute(
        text("SELECT 1 FROM hr.employee WHERE security_user_id = CAST(:u AS uuid)"),
        {"u": found.user_id},
    ).first()
    if taken:
        raise conflict("LOGIN_IN_USE", "That login is already linked to another employee.")
    conn.execute(
        text(
            "UPDATE hr.employee SET security_user_id = CAST(:u AS uuid), login_status = 'CREATED',"
            " login_error_code = NULL, updated_at = now(), updated_by = :a"
            " WHERE employee_id = CAST(:id AS uuid)"
        ),
        {"u": found.user_id, "a": user.user_id, "id": employee_id},
    )
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="LOGIN_LINKED",
        entity_type="employee",
        entity_id=employee_id,
        changes={"securityUserId": found.user_id, "matchedBy": "email"},
        request=request,
    )
    return {"employee": _view(_fetch(conn, employee_id))}


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
    return _detail(conn, _uuid(employee_id))


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


class UnknownDegree(Exception):
    pass


def _insert_qualification(
    conn: Connection, employee_id: str, q: QualificationIn, actor: str
) -> str:
    known = conn.execute(
        text("SELECT 1 FROM hr.degree WHERE code = :c AND active"), {"c": q.degree_code}
    ).first()
    if known is None:
        raise UnknownDegree(q.degree_code)
    return str(
        conn.execute(
            text(
                """
                INSERT INTO hr.employee_qualification
                    (employee_id, degree_code, degree_other, percentage, year_of_passing,
                     created_by, updated_by)
                VALUES (CAST(:e AS uuid), :d, :o, :p, :y, :a, :a)
                RETURNING qualification_id
                """
            ),
            {
                "e": employee_id,
                "d": q.degree_code,
                "o": q.degree_other,
                "p": q.percentage,
                "y": q.year_of_passing,
                "a": actor,
            },
        ).scalar_one()
    )


def _qualifications(conn: Connection, employee_id: str) -> list[dict[str, Any]]:
    rows = (
        conn.execute(
            text(
                """
                SELECT q.qualification_id, q.degree_code, d.label, d.level, q.degree_other,
                       q.percentage, q.year_of_passing
                FROM hr.employee_qualification q JOIN hr.degree d ON d.code = q.degree_code
                WHERE q.employee_id = CAST(:e AS uuid)
                ORDER BY q.year_of_passing DESC, q.created_at
                """
            ),
            {"e": employee_id},
        )
        .mappings()
        .all()
    )
    return [
        {
            "qualificationId": str(r["qualification_id"]),
            "degreeCode": r["degree_code"],
            "degree": r["degree_other"] if r["degree_code"] == "OTHER" else r["label"],
            "level": r["level"],
            "percentage": float(r["percentage"]),
            "yearOfPassing": r["year_of_passing"],
        }
        for r in rows
    ]


def _detail(conn: Connection, employee_id: str) -> dict[str, Any]:
    out = _view(_fetch(conn, employee_id))
    out["qualifications"] = _qualifications(conn, employee_id)
    return out


@router.get("/degrees")
def degrees(
    _: HumanPrincipal = Depends(current_user), conn: Connection = Depends(get_conn)
) -> list[dict[str, str]]:
    rows = conn.execute(
        text("SELECT code, label, level FROM hr.degree WHERE active ORDER BY sort_order")
    ).mappings()
    return [{"code": r["code"], "label": r["label"], "level": r["level"]} for r in rows]


@router.get("/states")
def states(_: HumanPrincipal = Depends(current_user)) -> list[str]:
    return list(STATES)


@router.post("/employees/{employee_id}/qualifications", status_code=201)
def add_qualification(
    employee_id: str,
    body: QualificationIn,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _uuid(employee_id)
    _fetch(conn, employee_id)
    try:
        qid = _insert_qualification(conn, employee_id, body, user.user_id)
    except UnknownDegree as exc:
        raise conflict("DEGREE_UNKNOWN", "Choose a degree from the list.") from exc
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="QUALIFICATION_ADDED",
        entity_type="employee",
        entity_id=employee_id,
        changes={
            "qualificationId": qid,
            "degree": body.degree_code,
            "percentage": str(body.percentage),
            "yearOfPassing": body.year_of_passing,
        },
        request=request,
    )
    return _detail(conn, employee_id)


@router.put("/employees/{employee_id}/qualifications/{qualification_id}")
def replace_qualification(
    employee_id: str,
    qualification_id: str,
    body: QualificationIn,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id, qualification_id = _uuid(employee_id), _uuid(qualification_id)
    if (
        conn.execute(
            text("SELECT 1 FROM hr.degree WHERE code = :c AND active"),
            {"c": body.degree_code},
        ).first()
        is None
    ):
        raise conflict("DEGREE_UNKNOWN", "Choose a degree from the list.")
    updated = conn.execute(
        text(
            """
            UPDATE hr.employee_qualification
            SET degree_code = :d, degree_other = :o, percentage = :p, year_of_passing = :y,
                updated_at = now(), updated_by = :a
            WHERE qualification_id = CAST(:q AS uuid) AND employee_id = CAST(:e AS uuid)
            """
        ),
        {
            "d": body.degree_code,
            "o": body.degree_other,
            "p": body.percentage,
            "y": body.year_of_passing,
            "a": user.user_id,
            "q": qualification_id,
            "e": employee_id,
        },
    ).rowcount
    if not updated:
        raise not_found("Qualification not found.")
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="QUALIFICATION_UPDATED",
        entity_type="employee",
        entity_id=employee_id,
        changes={
            "qualificationId": qualification_id,
            "degree": body.degree_code,
            "percentage": str(body.percentage),
            "yearOfPassing": body.year_of_passing,
        },
        request=request,
    )
    return _detail(conn, employee_id)


@router.delete("/employees/{employee_id}/qualifications/{qualification_id}")
def delete_qualification(
    employee_id: str,
    qualification_id: str,
    request: Request,
    user: HumanPrincipal = Depends(can_manage),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id, qualification_id = _uuid(employee_id), _uuid(qualification_id)
    removed = conn.execute(
        text(
            "DELETE FROM hr.employee_qualification"
            " WHERE qualification_id = CAST(:q AS uuid) AND employee_id = CAST(:e AS uuid)"
        ),
        {"q": qualification_id, "e": employee_id},
    ).rowcount
    if not removed:
        raise not_found("Qualification not found.")
    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="QUALIFICATION_REMOVED",
        entity_type="employee",
        entity_id=employee_id,
        changes={"qualificationId": qualification_id},
        request=request,
    )
    return _detail(conn, employee_id)


# ---- Profile photo (optional). Re-encoded to a small JPEG with metadata removed, kept in the
# object store under hr/, served only through this service after the permission check. ----------


def get_storage(request: Request) -> ObjectStorage | None:
    return getattr(request.app.state, "storage", None)


def _photo_key(employee_id: str) -> str:
    return f"employee/{employee_id}/photo.jpg"


def _save_photo(
    conn: Connection,
    *,
    employee_id: str,
    data: bytes,
    storage: ObjectStorage | None,
    actor: str,
    request: Request,
) -> dict[str, Any]:
    if storage is None:
        raise dependency_unavailable("Photo storage is not configured.")
    try:
        jpeg = normalise_profile_photo(data)
    except PhotoError as exc:
        raise ApiError(422, "HR_PHOTO_NOT_ACCEPTED", str(exc)) from exc
    try:
        storage.put(_photo_key(employee_id), jpeg, "image/jpeg")
    except StorageError as exc:
        raise dependency_unavailable("The photo could not be saved. Please try again.") from exc
    conn.execute(
        text(
            "UPDATE hr.employee SET photo_updated_at = now(), updated_at = now(), updated_by = :a"
            " WHERE employee_id = CAST(:id AS uuid)"
        ),
        {"a": actor, "id": employee_id},
    )
    record_audit(
        conn,
        actor_user_id=actor,
        action="PHOTO_CHANGED",
        entity_type="employee",
        entity_id=employee_id,
        changes={"photo": "changed"},
        request=request,
    )
    return _view(_fetch(conn, employee_id))


def _read_photo(conn: Connection, employee_id: str, storage: ObjectStorage | None) -> Response:
    row = _fetch(conn, employee_id)
    if row["photo_updated_at"] is None:
        raise not_found("No photo.")
    if storage is None:
        raise dependency_unavailable("Photo storage is not configured.")
    try:
        data = storage.get(_photo_key(employee_id))
    except StorageError as exc:
        raise dependency_unavailable("The photo could not be loaded.") from exc
    return Response(
        content=data, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"}
    )


async def _upload_bytes(file: UploadFile) -> bytes:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    return data


@router.post("/employees/{employee_id}/photo")
async def set_employee_photo(
    employee_id: str,
    request: Request,
    file: UploadFile = File(...),
    user: HumanPrincipal = Depends(can_manage),
    storage: ObjectStorage | None = Depends(get_storage),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _uuid(employee_id)
    _fetch(conn, employee_id)
    return _save_photo(
        conn,
        employee_id=employee_id,
        data=await _upload_bytes(file),
        storage=storage,
        actor=user.user_id,
        request=request,
    )


@router.get("/employees/{employee_id}/photo")
def get_employee_photo(
    employee_id: str,
    _: HumanPrincipal = Depends(can_read),
    storage: ObjectStorage | None = Depends(get_storage),
    conn: Connection = Depends(get_conn),
) -> Response:
    return _read_photo(conn, _uuid(employee_id), storage)


@router.post("/me/employee/photo")
async def set_my_photo(
    request: Request,
    file: UploadFile = File(...),
    user: HumanPrincipal = Depends(current_user),
    storage: ObjectStorage | None = Depends(get_storage),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    employee_id = _own_employee_id(conn, user)
    return _save_photo(
        conn,
        employee_id=employee_id,
        data=await _upload_bytes(file),
        storage=storage,
        actor=user.user_id,
        request=request,
    )


@router.get("/me/employee/photo")
def get_my_photo(
    user: HumanPrincipal = Depends(current_user),
    storage: ObjectStorage | None = Depends(get_storage),
    conn: Connection = Depends(get_conn),
) -> Response:
    return _read_photo(conn, _own_employee_id(conn, user), storage)


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
    return _detail(conn, _own_employee_id(conn, user))


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
