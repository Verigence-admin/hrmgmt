from __future__ import annotations

import hashlib
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import ValidationError
from sqlalchemy import Connection, text

from hrmgmt import permissions as perm
from hrmgmt import validators as v
from hrmgmt.api.employees import (
    EmployeeCreate,
    QualificationIn,
    create_employee_record,
    get_provisioner,
)
from hrmgmt.audit import record_audit
from hrmgmt.db import get_conn
from hrmgmt.errors import ApiError
from hrmgmt.importer import MAX_IMPORT_BYTES, ImportFileError, ParsedRow, parse_employee_sheet
from hrmgmt.principal import require_permission
from hrmgmt.provisioning import UserProvisioner
from hrmgmt.security import HumanPrincipal

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/hr/v1", tags=["Employee import"])

can_manage = require_permission(perm.HR_EMPLOYEE_MANAGE)

# Each call creates at most this many employees, so one request stays short even when every
# row needs a Security login. The screen sends the rows in successive calls.
MAX_ROWS_PER_COMMIT = 10


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise ApiError(422, "HR_IMPORT_FILE_INVALID", "The file is larger than 2 MB.")
    return data


def _parse(data: bytes) -> list[ParsedRow]:
    try:
        return parse_employee_sheet(data)
    except ImportFileError as exc:
        raise ApiError(422, "HR_IMPORT_FILE_INVALID", str(exc)) from exc


def _existing(conn: Connection, rows: list[ParsedRow]) -> tuple[set[str], set[str], set[str]]:
    """Codes, emails and PANs already on file (one query each, for the whole sheet)."""
    codes = [r.values["employee_code"] for r in rows if r.values["employee_code"]]
    emails = [r.values["personal_email"] for r in rows if r.values["personal_email"]]
    pans = [r.values["pan"] for r in rows if r.values["pan"]]

    def column(sql: str, items: list[str]) -> set[str]:
        if not items:
            return set()
        return {str(x[0]) for x in conn.execute(text(sql), {"items": items})}

    return (
        column("SELECT employee_code FROM hr.employee WHERE employee_code = ANY(:items)", codes),
        column("SELECT personal_email FROM hr.employee WHERE personal_email = ANY(:items)", emails),
        column("SELECT pan FROM hr.employee_sensitive WHERE pan = ANY(:items)", pans),
    )


def _degrees(conn: Connection) -> dict[str, str]:
    """Catalogue degrees by lower-case code and label, so a sheet can name one either way."""
    found: dict[str, str] = {}
    for r in conn.execute(text("SELECT code, label FROM hr.degree")):
        found[str(r[0]).lower()] = str(r[0])
        found[str(r[1]).lower()] = str(r[0])
    return found


def _qualification(row: ParsedRow, degrees: dict[str, str]) -> QualificationIn | None:
    """One structured qualification, only when the degree is in the catalogue and the marks and
    year are given. Otherwise nothing is guessed; the row is noted and HR adds it on the page."""
    values = row.values
    if not (values["degree"] or values["university"] or values["college"]):
        return None
    code = degrees.get((values["degree"] or "").lower())
    if code is None or values["percentage"] is None or values["year_of_passing"] is None:
        return None
    return QualificationIn(
        degree_code=code,
        percentage=values["percentage"],
        year_of_passing=values["year_of_passing"],
        university=values["university"],
        college=values["college"],
    )


def _qualification_note(row: ParsedRow, degrees: dict[str, str]) -> str | None:
    values = row.values
    if not (values["degree"] or values["university"] or values["college"]):
        return None
    if _qualification(row, degrees) is None:
        return (
            "Degree (as in the list), percentage and year of passing are all needed to save the "
            "qualification, university and college; add them on the employee's page."
        )
    return None


def _status(row: ParsedRow, codes: set[str], emails: set[str]) -> str:
    if row.errors:
        return "ERROR"
    if row.values["employee_code"] in codes:
        return "EXISTS"
    if row.values["personal_email"] in emails:
        return "ERROR"
    return "READY"


@router.post("/employees/import/preview")
async def preview_import(
    file: UploadFile = File(...),
    _: HumanPrincipal = Depends(can_manage),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Read the sheet and say what would happen. Nothing is saved."""
    rows = _parse(await _read_upload(file))
    codes, emails, pans = _existing(conn, rows)
    degrees = _degrees(conn)
    file_pans: dict[str, int] = {}
    for r in rows:
        if r.values["pan"]:
            file_pans[r.values["pan"]] = file_pans.get(r.values["pan"], 0) + 1
    out = []
    for r in rows:
        status = _status(r, codes, emails)
        notes = list(r.notes)
        errors = list(r.errors)
        if status == "EXISTS":
            errors = ["An employee with this ID already exists; the row is skipped."]
        elif status == "ERROR" and not errors:
            errors = ["This email already belongs to an employee."]
        pan = r.values["pan"]
        if pan and (pan in pans or file_pans[pan] > 1):
            notes.append("PAN is also used by another employee; saved and flagged for HR.")
        qualification_note = _qualification_note(r, degrees)
        if qualification_note:
            notes.append(qualification_note)
        out.append(
            {
                "row": r.row,
                "status": status,
                "employeeCode": r.values["employee_code"],
                "fullName": r.values["full_name"],
                "personalEmail": r.values["personal_email"],
                "mobile": r.values["mobile"],
                "dateOfBirth": r.values["date_of_birth"].isoformat()
                if r.values["date_of_birth"]
                else None,
                "gender": r.values["gender"],
                "department": r.values["department"],
                "qualification": r.values["qualification"],
                "state": r.values["state"],
                "district": r.values["district"],
                "pincode": r.values["pincode"],
                "panMasked": v.mask_pan(pan),
                "aadhaarMasked": v.mask_aadhaar(r.values["aadhaar"]),
                "notes": notes,
                "errors": errors,
            }
        )
    count = {s: sum(1 for o in out if o["status"] == s) for s in ("READY", "EXISTS", "ERROR")}
    return {
        "summary": {
            "total": len(out),
            "ready": count["READY"],
            "exists": count["EXISTS"],
            "errors": count["ERROR"],
        },
        "rows": out,
    }


@router.post("/employees/import/commit")
async def commit_import(
    request: Request,
    file: UploadFile = File(...),
    rows: Annotated[str, Form(max_length=200)] = "",
    create_login: Annotated[bool, Form()] = True,
    user: HumanPrincipal = Depends(can_manage),
    provisioner: UserProvisioner | None = Depends(get_provisioner),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Create the listed rows (at most 10 per call). The sheet is read again here, so what is
    saved is what the sheet says now, checked again against what is already on file."""
    try:
        wanted = sorted({int(x) for x in rows.split(",") if x.strip()})
    except ValueError as exc:
        raise ApiError(422, "HR_VALIDATION_FAILED", "Rows must be a list of row numbers.") from exc
    if not wanted:
        raise ApiError(422, "HR_VALIDATION_FAILED", "Choose at least one row to import.")
    if len(wanted) > MAX_ROWS_PER_COMMIT:
        raise ApiError(
            422, "HR_VALIDATION_FAILED", f"Import at most {MAX_ROWS_PER_COMMIT} rows at a time."
        )
    data = await _read_upload(file)
    parsed = {r.row: r for r in _parse(data)}
    codes, emails, _ = _existing(conn, list(parsed.values()))
    degrees = _degrees(conn)

    results: list[dict[str, Any]] = []
    for number in wanted:
        row = parsed.get(number)
        if row is None:
            results.append(
                {"row": number, "status": "FAILED", "error": "Row not found in the file."}
            )
            continue
        status = _status(row, codes, emails)
        base = {"row": number, "employeeCode": row.values["employee_code"]}
        if status != "READY":
            results.append({**base, "status": "SKIPPED", "error": "Not ready to import."})
            continue
        try:
            body = EmployeeCreate(
                employee_code=row.values["employee_code"],
                full_name=row.values["full_name"],
                personal_email=row.values["personal_email"],
                mobile=row.values["mobile"],
                date_of_birth=row.values["date_of_birth"],
                gender=row.values["gender"],
                qualification=row.values["qualification"],
                department=row.values["department"],
                address=row.values["address"],
                state=row.values["state"],
                district=row.values["district"],
                pincode=row.values["pincode"],
                total_experience_years=row.values["experience"],
                emergency_contact_name=row.values["emergency_name"],
                emergency_contact_number=row.values["emergency_number"],
                qualifications=[q] if (q := _qualification(row, degrees)) else [],
                date_of_joining=row.values["date_of_joining"],
                pan=row.values["pan"],
                aadhaar=row.values["aadhaar"],
                create_login=create_login,
            )
            created = create_employee_record(
                conn, body=body, actor=user.user_id, provisioner=provisioner, request=request
            )
            conn.commit()
        except (ApiError, ValidationError) as exc:
            conn.rollback()
            message = exc.detail if isinstance(exc, ApiError) else "Some details are not valid."
            results.append({**base, "status": "FAILED", "error": message})
            continue
        except Exception as exc:  # one bad row must not stop the others
            conn.rollback()
            logger.error("hr_import_row_failed", row=number, error_type=type(exc).__name__)
            results.append({**base, "status": "FAILED", "error": "The row could not be saved."})
            continue
        employee = created["employee"]
        item: dict[str, Any] = {
            **base,
            "status": "CREATED",
            "employeeId": employee["employeeId"],
            "fullName": employee["fullName"],
            "personalEmail": employee["personalEmail"],
            "loginStatus": employee["loginStatus"],
            "loginErrorCode": employee["loginErrorCode"],
        }
        if "initialPassword" in created:
            item["initialPassword"] = created["initialPassword"]
        results.append(item)

    record_audit(
        conn,
        actor_user_id=user.user_id,
        action="EMPLOYEES_IMPORTED",
        entity_type="employee_import",
        entity_id=hashlib.sha256(data).hexdigest()[:16],
        changes={
            "requested": len(wanted),
            "created": sum(1 for r in results if r["status"] == "CREATED"),
            "failed": sum(1 for r in results if r["status"] == "FAILED"),
            "createLogin": create_login,
        },
        request=request,
    )
    return {"results": results}
