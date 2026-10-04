"""Reads an employee spreadsheet (.xlsx) into rows ready to be created.

Only what the sheet says is read; nothing is guessed. A value that is not valid (a mobile with
nine digits, a PAN with the wrong shape) is left empty and noted, so the employee can still be
created and HR fixes it later. Only a missing employee code, name or email stops a row.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from openpyxl import load_workbook

from hrmgmt import validators as v
from hrmgmt.catalog import canonical_state

MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_SHEET_ROWS = 1000  # rows read; an empty-but-formatted sheet can claim far more

_ALIASES: dict[str, str] = {
    "employee id": "employee_code",
    "employee code": "employee_code",
    "emp id": "employee_code",
    "emp code": "employee_code",
    "employee name": "full_name",
    "name": "full_name",
    "full name": "full_name",
    "dob": "date_of_birth",
    "date of birth": "date_of_birth",
    "gender": "gender",
    "contact number": "mobile",
    "contact no": "mobile",
    "mobile": "mobile",
    "mobile number": "mobile",
    "mobile no": "mobile",
    "phone": "mobile",
    "personal email": "personal_email",
    "email": "personal_email",
    "email id": "personal_email",
    "qualification": "qualification",
    "department": "department",
    "pan no": "pan",
    "pan": "pan",
    "pan number": "pan",
    "aadhar no": "aadhaar",
    "aadhaar no": "aadhaar",
    "aadhar": "aadhaar",
    "aadhaar": "aadhaar",
    "aadhar number": "aadhaar",
    "aadhaar number": "aadhaar",
    "address": "address",
    "state": "state",
    "district": "district",
    "pincode": "pincode",
    "pin code": "pincode",
    "years of experience": "experience",
    "experience": "experience",
    "total experience": "experience",
    "emergency contact name": "emergency_name",
    "emergency contact number": "emergency_number",
    "emergency contact": "emergency_number",
    "degree": "degree",
    "percentage": "percentage",
    "year of passing": "year_of_passing",
    "passing year": "year_of_passing",
    "university": "university",
    "college": "college",
    "college name": "college",
    "date of joining": "date_of_joining",
    "doj": "date_of_joining",
}
REQUIRED_COLUMNS = ("employee_code", "full_name", "personal_email")

_DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y")
_GENDERS = {"male": "MALE", "m": "MALE", "female": "FEMALE", "f": "FEMALE", "other": "OTHER"}


class ImportFileError(ValueError):
    """The file cannot be read as an employee sheet. The message is safe to show."""


@dataclass
class ParsedRow:
    row: int
    values: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # saved anyway, with something to fix later
    errors: list[str] = field(default_factory=list)  # the row cannot be created


def _normal_header(raw: Any) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", str(raw or "").lower())
    return " ".join(text.split())


def _text(cell: Any) -> str:
    if cell is None:
        return ""
    if isinstance(cell, float) and cell.is_integer():
        cell = int(cell)
    return " ".join(str(cell).split())


def _date(cell: Any) -> date | None:
    if isinstance(cell, datetime):
        return cell.date()
    if isinstance(cell, date):
        return cell
    raw = _text(cell)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _read_sheet(data: bytes) -> list[tuple[int, dict[str, Any]]]:
    if len(data) > MAX_IMPORT_BYTES:
        raise ImportFileError("The file is larger than 2 MB.")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise ImportFileError("Upload an Excel .xlsx file.")
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises many types for a damaged file
        raise ImportFileError("This file could not be read as an Excel workbook.") from exc
    try:
        sheet = workbook[workbook.sheetnames[0]]
        header: dict[int, str] | None = None
        rows: list[tuple[int, dict[str, Any]]] = []
        for number, cells in enumerate(
            sheet.iter_rows(max_row=MAX_SHEET_ROWS, values_only=True), 1
        ):
            if header is None:
                mapped = {
                    i: _ALIASES[_normal_header(c)]
                    for i, c in enumerate(cells)
                    if _normal_header(c) in _ALIASES
                }
                if all(col in mapped.values() for col in REQUIRED_COLUMNS):
                    header = mapped
                continue
            record = {header[i]: cells[i] for i in header if i < len(cells)}
            if all(_text(value) == "" for value in record.values()):
                continue  # a blank row
            rows.append((number, record))
    finally:
        workbook.close()
    if header is None:
        raise ImportFileError(
            "No header row found. The sheet needs columns for Employee ID, Employee Name and "
            "Personal Email."
        )
    return rows


def _read_onboarding_details(record: dict[str, Any], row: ParsedRow) -> None:
    """State, district, pincode, experience, emergency contact and one qualification. A value that
    is not valid is left empty and noted; a missing district, university or college is noted so
    HR fills it in later."""
    values = row.values

    state = _text(record.get("state"))
    values["state"] = None
    if state:
        try:
            values["state"] = canonical_state(state)
        except ValueError:
            row.notes.append("State is not recognised; left empty.")
    district = _text(record.get("district"))
    values["district"] = district[:80] if district else None
    if not district:
        row.notes.append("District is missing.")

    pincode = _text(record.get("pincode"))
    values["pincode"] = None
    if pincode:
        if len(pincode) == 6 and pincode.isdigit() and pincode[0] != "0":
            values["pincode"] = pincode
        else:
            row.notes.append("Pincode is not 6 digits; left empty.")

    experience = _text(record.get("experience"))
    values["experience"] = None
    if experience:
        try:
            years = Decimal(experience)
            if years < 0 or years > 60:
                raise ValueError
            values["experience"] = years.quantize(Decimal("0.1"))
        except (InvalidOperation, ValueError):
            row.notes.append("Years of experience is not a valid number; left empty.")

    values["emergency_name"] = _text(record.get("emergency_name"))[:120] or None
    emergency = _text(record.get("emergency_number"))
    values["emergency_number"] = None
    if emergency:
        try:
            values["emergency_number"] = v.clean_indian_mobile(emergency)
        except ValueError:
            row.notes.append("Emergency contact number is not a valid Indian mobile; left empty.")

    values["degree"] = _text(record.get("degree"))[:120] or None
    values["university"] = _text(record.get("university"))[:150] or None
    values["college"] = _text(record.get("college"))[:150] or None
    if not values["university"]:
        row.notes.append("University is missing.")
    if not values["college"]:
        row.notes.append("College name is missing.")
    values["percentage"] = None
    percentage = _text(record.get("percentage")).rstrip("%").strip()
    if percentage:
        try:
            marks = Decimal(percentage)
            if marks < 0 or marks > 100:
                raise ValueError
            values["percentage"] = marks.quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            row.notes.append("Percentage is not valid; left empty.")
    values["year_of_passing"] = None
    year = _text(record.get("year_of_passing"))
    if year:
        if year.isdigit() and 1950 <= int(year) <= date.today().year:
            values["year_of_passing"] = int(year)
        else:
            row.notes.append("Year of passing is not valid; left empty.")


def parse_employee_sheet(data: bytes) -> list[ParsedRow]:
    parsed: list[ParsedRow] = []
    for number, record in _read_sheet(data):
        row = ParsedRow(row=number)
        values = row.values

        code = _text(record.get("employee_code")).upper()
        if not re.fullmatch(r"[A-Z0-9-]{2,20}", code):
            row.errors.append("Employee ID is missing or not valid (letters, digits and hyphens).")
        values["employee_code"] = code

        name = _text(record.get("full_name"))
        if len(name) < 2:
            row.errors.append("Employee name is missing.")
        values["full_name"] = name

        email = _text(record.get("personal_email"))
        try:
            values["personal_email"] = v.clean_email(email)
        except ValueError:
            values["personal_email"] = None
            row.errors.append("Personal email is missing or not valid.")

        mobile = _text(record.get("mobile"))
        values["mobile"] = None
        if mobile:
            try:
                values["mobile"] = v.clean_indian_mobile(mobile)
            except ValueError:
                row.notes.append("Mobile number is not a valid Indian mobile; left empty.")
        else:
            row.notes.append("No mobile number; the Verigence login needs one.")

        dob_cell = record.get("date_of_birth")
        values["date_of_birth"] = None
        if _text(dob_cell):
            dob = _date(dob_cell)
            if dob is None or dob >= date.today():
                row.notes.append("Date of birth is not valid; left empty.")
            else:
                values["date_of_birth"] = dob

        joined_cell = record.get("date_of_joining")
        values["date_of_joining"] = None
        if _text(joined_cell):
            joined = _date(joined_cell)
            if joined is None:
                row.notes.append("Date of joining is not valid; left empty.")
            else:
                values["date_of_joining"] = joined

        gender_raw = _text(record.get("gender"))
        values["gender"] = _GENDERS.get(gender_raw.lower()) if gender_raw else None
        if gender_raw and values["gender"] is None:
            row.notes.append("Gender is not recognised; left empty.")

        for key, limit in (("qualification", 120), ("department", 60), ("address", 500)):
            text = _text(record.get(key))
            values[key] = text[:limit] if text else None
            if len(text) > limit:
                row.notes.append(
                    f"{key.capitalize()} was longer than {limit} characters; shortened."
                )

        _read_onboarding_details(record, row)

        pan = _text(record.get("pan"))
        values["pan"] = None
        if pan:
            try:
                values["pan"] = v.clean_pan(pan)
            except ValueError:
                row.notes.append("PAN is not in the PAN format; left empty.")
        else:
            row.notes.append("PAN is missing.")

        aadhaar = _text(record.get("aadhaar"))
        values["aadhaar"] = None
        if aadhaar:
            try:
                values["aadhaar"] = v.clean_aadhaar(aadhaar)
            except ValueError:
                row.notes.append("Aadhaar is not 12 digits; left empty.")
        else:
            row.notes.append("Aadhaar is missing.")

        parsed.append(row)

    seen_codes: dict[str, int] = {}
    seen_emails: dict[str, int] = {}
    for row in parsed:
        code = row.values["employee_code"]
        if code:
            if code in seen_codes:
                row.errors.append(
                    f"Employee ID is repeated in this file (also row {seen_codes[code]})."
                )
            else:
                seen_codes[code] = row.row
        email = row.values["personal_email"]
        if email:
            if email in seen_emails:
                row.errors.append(
                    f"Email is repeated in this file (also row {seen_emails[email]})."
                )
            else:
                seen_emails[email] = row.row
    return parsed
