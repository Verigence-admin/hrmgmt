from __future__ import annotations

import io
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import text

from hrmgmt.importer import ImportFileError, parse_employee_sheet
from hrmgmt.main import create_app
from tests.test_api import FakeAuthorizer, FakeValidator
from tests.test_employees import (
    GRANTS,
    HR,
    HR_READER,
    FakeProvisioner,
    FakeStorage,
    _settings,
    auth,
    payload,
)


@pytest.fixture()
def make_client(migrated_engine):
    def build() -> tuple[TestClient, FakeProvisioner]:
        app = create_app(_settings())
        app.state.validator = FakeValidator()
        app.state.authorizer = FakeAuthorizer(GRANTS)
        provisioner = FakeProvisioner()
        app.state.provisioner = provisioner
        app.state.storage = FakeStorage()
        return TestClient(app, raise_server_exceptions=False), provisioner

    return build


HEADER = [
    "Sl. No.",
    "Employee Name",
    "DOB",
    "GENDER",
    "Contact Number",
    "Personal Email",
    "Qualification",
    "Employee ID",
    "Department",
    "Pan No.",
    "Aadhar No.",
    "Address",
]


def sheet(rows: list[list], header: list | None = None, blank_tail: int = 3) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(header or HEADER)
    for r in rows:
        ws.append(r)
    for _ in range(blank_tail):
        ws.append([None] * len(HEADER))
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def good_row(n: str | None = None, **over):
    n = n or uuid.uuid4().hex[:6].upper()
    row = {
        "sl": 1,
        "name": "Asha Rao",
        "dob": datetime(1995, 4, 2),
        "gender": "Female",
        "mobile": int("9" + str(uuid.uuid4().int)[:9]),  # each employee's mobile is their own
        "email": f"asha.{n.lower()}@example.com",
        "qual": "B com,MBA",
        "code": f"IM{n}",
        "dept": "Audit",
        "pan": "ABCDE1234F",
        "aadhaar": 123456789012,
        "address": "12 Main Road",
    }
    row.update(over)
    return list(row.values())


def upload(client, path: str, data: bytes, who: str = HR, **form):
    return client.post(
        path,
        headers=auth(who),
        files={
            "file": (
                "e.xlsx",
                data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data=form,
    )


# ---- parsing -------------------------------------------------------------------------------


def test_parser_reads_numbers_dates_and_skips_blank_rows():
    rows = parse_employee_sheet(sheet([good_row("A1", mobile=9876543210)]))
    assert len(rows) == 1
    v = rows[0].values
    assert v["employee_code"] == "IMA1" and v["mobile"] == "9876543210"
    assert v["aadhaar"] == "123456789012" and v["gender"] == "FEMALE"
    assert v["date_of_birth"].isoformat() == "1995-04-02"
    assert v["qualification"] == "B com,MBA" and rows[0].errors == []
    assert rows[0].notes == [
        "District is missing.",
        "University is missing.",
        "College name is missing.",
    ]
    assert rows[0].row == 2  # the sheet row number, shown to the person


def test_invalid_values_become_empty_notes_not_errors():
    rows = parse_employee_sheet(
        sheet([good_row("B1", mobile=12345, pan="XYZ", aadhaar=1234, dob="not a date")])
    )
    r = rows[0]
    assert r.errors == []
    assert r.values["mobile"] is None and r.values["pan"] is None and r.values["aadhaar"] is None
    assert r.values["date_of_birth"] is None
    joined = " ".join(r.notes)
    assert "mobile" in joined.lower() and "PAN" in joined and "Aadhaar" in joined


def test_missing_pan_is_a_note_and_missing_email_is_an_error():
    r = parse_employee_sheet(sheet([good_row("C1", pan=None, email=None)]))[0]
    assert any("PAN is missing" in n for n in r.notes)
    assert r.values["personal_email"] is None and r.errors


def test_repeats_inside_the_file_are_errors():
    rows = parse_employee_sheet(
        sheet(
            [
                good_row("D1", code="SAME1", email="x@example.com"),
                good_row("D2", code="SAME1", email="x@example.com"),
            ]
        )
    )
    assert rows[0].errors == []
    assert len(rows[1].errors) == 2


def test_header_aliases_and_missing_required_columns():
    ok = sheet([["JBR1", "Asha", "a@example.com"]], header=["Employee Code", "Name", "Email"])
    assert parse_employee_sheet(ok)[0].values["employee_code"] == "JBR1"
    with pytest.raises(ImportFileError):
        parse_employee_sheet(sheet([["a"]], header=["Foo"]))


def test_non_excel_and_oversized_files_are_refused():
    with pytest.raises(ImportFileError):
        parse_employee_sheet(b"not a zip")
    with pytest.raises(ImportFileError):
        parse_employee_sheet(b"PK" + b"0" * (2 * 1024 * 1024 + 1))


# ---- preview -------------------------------------------------------------------------------


def test_preview_reports_each_row_without_saving_or_revealing_pan(make_client, migrated_engine):
    client, prov = make_client()
    data = sheet([good_row("E1"), good_row("E2", pan=None, mobile=None)])
    r = upload(client, "/hr/v1/employees/import/preview", data)
    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == {"total": 2, "ready": 2, "exists": 0, "errors": 0}
    first = body["rows"][0]
    assert first["panMasked"] == "XXXXX1234F" and first["aadhaarMasked"] == "XXXX XXXX 9012"
    assert "ABCDE1234F" not in r.text and "123456789012" not in r.text
    assert any("PAN is missing" in n for n in body["rows"][1]["notes"])
    assert prov.calls == []
    with migrated_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM hr.employee WHERE employee_code IN ('IME1','IME2')")
            ).scalar_one()
            == 0
        )


def test_preview_flags_existing_codes_and_emails(make_client):
    client, _ = make_client()
    existing = client.post("/hr/v1/employees", json=payload(), headers=auth(HR)).json()["employee"]
    data = sheet(
        [
            good_row("F1", code=existing["employeeCode"]),
            good_row("F2", email=existing["personalEmail"]),
            good_row("F3"),
        ]
    )
    rows = upload(client, "/hr/v1/employees/import/preview", data).json()["rows"]
    assert [r["status"] for r in rows] == ["EXISTS", "ERROR", "READY"]
    assert "already belongs" in rows[1]["errors"][0]


def test_preview_notes_a_pan_already_used(make_client):
    client, _ = make_client()
    client.post("/hr/v1/employees", json=payload(pan="QWERT1234Y"), headers=auth(HR))
    rows = upload(
        client, "/hr/v1/employees/import/preview", sheet([good_row("G1", pan="QWERT1234Y")])
    ).json()["rows"]
    assert rows[0]["status"] == "READY"
    assert any("also used" in n for n in rows[0]["notes"])


def test_import_needs_manage_permission(make_client):
    client, _ = make_client()
    data = sheet([good_row("H1")])
    assert upload(client, "/hr/v1/employees/import/preview", data, who=HR_READER).status_code == 403
    assert (
        upload(client, "/hr/v1/employees/import/commit", data, who=HR_READER, rows="2").status_code
        == 403
    )


def test_bad_file_is_a_clear_422(make_client):
    client, _ = make_client()
    r = upload(client, "/hr/v1/employees/import/preview", b"hello")
    assert r.status_code == 422 and r.json()["code"] == "HR_IMPORT_FILE_INVALID"


# ---- commit --------------------------------------------------------------------------------


def test_commit_creates_rows_with_logins_and_shows_passwords_once(make_client, migrated_engine):
    client, prov = make_client()
    data = sheet([good_row("J1"), good_row("J2", pan=None, aadhaar=None)])
    r = upload(client, "/hr/v1/employees/import/commit", data, rows="2,3", create_login="true")
    assert r.status_code == 200
    results = r.json()["results"]
    assert [x["status"] for x in results] == ["CREATED", "CREATED"]
    assert all(x["loginStatus"] == "CREATED" and len(x["initialPassword"]) >= 12 for x in results)
    assert len(prov.calls) == 2
    emp = client.get(f"/hr/v1/employees/{results[1]['employeeId']}", headers=auth(HR)).json()
    assert "PAN_MISSING" in emp["dataFlags"] and "AADHAAR_MISSING" in emp["dataFlags"]
    assert (
        results[0]["initialPassword"]
        not in client.get(f"/hr/v1/employees/{results[0]['employeeId']}", headers=auth(HR)).text
    )
    with migrated_engine.connect() as conn:
        actions = [
            a[0]
            for a in conn.execute(
                text("SELECT action FROM hr.audit_log WHERE action = 'EMPLOYEES_IMPORTED'")
            )
        ]
    assert actions


def test_commit_without_login_makes_no_security_call(make_client):
    client, prov = make_client()
    data = sheet([good_row("K1")])
    r = upload(client, "/hr/v1/employees/import/commit", data, rows="2", create_login="false")
    item = r.json()["results"][0]
    assert item["status"] == "CREATED" and item["loginStatus"] == "NOT_CREATED"
    assert "initialPassword" not in item and prov.calls == []


def test_commit_keeps_the_rest_when_one_row_fails(make_client):
    client, _ = make_client()
    existing = client.post("/hr/v1/employees", json=payload(), headers=auth(HR)).json()["employee"]
    data = sheet([good_row("L1", code=existing["employeeCode"]), good_row("L2")])
    results = upload(client, "/hr/v1/employees/import/commit", data, rows="2,3").json()["results"]
    assert [x["status"] for x in results] == ["SKIPPED", "CREATED"]


def test_commit_is_limited_and_validated(make_client):
    client, _ = make_client()
    data = sheet([good_row("M1")])
    many = ",".join(str(i) for i in range(2, 14))
    assert upload(client, "/hr/v1/employees/import/commit", data, rows=many).status_code == 422
    assert upload(client, "/hr/v1/employees/import/commit", data, rows="").status_code == 422
    assert upload(client, "/hr/v1/employees/import/commit", data, rows="x").status_code == 422


def test_running_the_same_commit_twice_does_not_duplicate(make_client):
    client, _ = make_client()
    data = sheet([good_row("N1")])
    first = upload(client, "/hr/v1/employees/import/commit", data, rows="2").json()["results"][0]
    second = upload(client, "/hr/v1/employees/import/commit", data, rows="2").json()["results"][0]
    assert first["status"] == "CREATED" and second["status"] == "SKIPPED"


# ---- onboarding columns --------------------------------------------------------------------

FULL_HEADER = [
    *HEADER,
    "State",
    "District",
    "Pincode",
    "Years of Experience",
    "Emergency Contact Name",
    "Emergency Contact Number",
    "Degree",
    "Percentage",
    "Year of Passing",
    "University",
    "College Name",
]


def full_row(n: str, **over):
    extra = {
        "state": "odisha",
        "district": "Cuttack",
        "pincode": 753001,
        "exp": 3.5,
        "ec_name": "Ravi Rao",
        "ec_number": 9123456789,
        "degree": "BCOM",
        "pct": 68,
        "year": 2020,
        "university": "Utkal University",
        "college": "Ravenshaw College",
    }
    extra.update(over)
    return good_row(n) + list(extra.values())


def test_parser_reads_the_onboarding_columns():
    r = parse_employee_sheet(sheet([full_row("F1")], header=FULL_HEADER))[0]
    v = r.values
    assert v["state"] == "Odisha" and v["district"] == "Cuttack" and v["pincode"] == "753001"
    assert str(v["experience"]) == "3.5" and v["emergency_number"] == "9123456789"
    assert v["university"] == "Utkal University" and v["college"] == "Ravenshaw College"
    assert v["year_of_passing"] == 2020 and str(v["percentage"]) == "68.00"
    assert r.errors == [] and r.notes == []


def test_missing_district_university_college_are_notes_and_bad_values_are_left_empty():
    r = parse_employee_sheet(
        sheet(
            [
                full_row(
                    "F2",
                    state="Atlantis",
                    district=None,
                    pincode=12,
                    university=None,
                    college=None,
                    ec_number=123,
                    exp=99,
                )
            ],
            header=FULL_HEADER,
        )
    )[0]
    assert r.errors == []
    v = r.values
    assert v["state"] is None and v["pincode"] is None and v["emergency_number"] is None
    assert v["experience"] is None
    joined = " ".join(r.notes)
    for word in ("District is missing", "University is missing", "College name is missing"):
        assert word in joined


def test_commit_saves_profile_and_one_qualification_when_the_degree_is_in_the_list(make_client):
    client, _ = make_client()
    data = sheet([full_row("G1"), full_row("G2", degree="Some Unknown Degree")], header=FULL_HEADER)
    pre = upload(client, "/hr/v1/employees/import/preview", data).json()["rows"]
    assert not any("qualification, university" in n for n in pre[0]["notes"])
    assert any("qualification, university" in n for n in pre[1]["notes"])
    r = upload(client, "/hr/v1/employees/import/commit", data, rows="2,3", create_login="false")
    first, second = r.json()["results"]
    emp = client.get(f"/hr/v1/employees/{first['employeeId']}", headers=auth(HR)).json()
    assert emp["district"] == "Cuttack" and emp["state"] == "Odisha"
    assert emp["totalExperienceYears"] == 3.5 and emp["emergencyContactNumber"] == "9123456789"
    assert emp["qualifications"][0]["university"] == "Utkal University"
    assert emp["qualifications"][0]["college"] == "Ravenshaw College"
    other = client.get(f"/hr/v1/employees/{second['employeeId']}", headers=auth(HR)).json()
    assert other["qualifications"] == [] and "QUALIFICATION" in other["missingDetails"]


def test_a_department_outside_the_five_is_noted_and_left_empty():
    ok, odd = parse_employee_sheet(
        sheet([good_row("D1", dept=" finance "), good_row("D2", dept="PC")])
    )
    assert ok.values["department"] == "Finance"
    assert odd.values["department"] is None
    assert any("Department is not Finance, CRM, HR, Audit or IT" in n for n in odd.notes)
