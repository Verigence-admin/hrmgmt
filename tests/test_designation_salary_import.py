from __future__ import annotations

import io
import uuid
from datetime import datetime

import pytest
from openpyxl import Workbook
from sqlalchemy import text

from hrmgmt import permissions as perm
from tests.support import World

HEADER = [
    "Sl. No.",
    "Employee ID",
    "Employee Name",
    "Designation",
    "Date of Joining",
    "Salary (Monthly)",
]
PATH = "/hr/v1/employees/designation-salary-import"


@pytest.fixture()
def world(migrated_engine):
    return World(migrated_engine)


def _sheet(rows: list[list]) -> bytes:
    book = Workbook()
    ws = book.active
    ws.append(HEADER)
    for r in rows:
        ws.append(r)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _post(world: World, user: str, which: str, data: bytes, **form):
    return world.client.post(
        f"{PATH}/{which}",
        headers=world.headers(user),
        files={"file": ("e.xlsx", data, "application/octet-stream")},
        data=form,
    )


def _setup(world: World):
    hr = world.grant(
        str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE, perm.HR_EMPLOYEE_READ, perm.HR_SALARY_PROPOSE
    )
    people = {}
    for name in ("plain", "band", "same", "odd"):
        code = f"DS{uuid.uuid4().hex[:6].upper()}"
        emp, _ = world.employee(hr, employee_code=code)
        people[name] = (code, emp["employeeId"])
    return hr, people


def _rows(people, extra=()):
    joined = datetime(2026, 9, 1)
    return [
        [1, people["plain"][0], "A", "Senior Analyst", joined, 40000],
        [2, people["band"][0], "B", "Analyst", joined, 23000],
        [3, people["same"][0], "C", "Consultant", joined, 60000],
        *extra,
    ]


def test_preview_says_what_would_happen_and_saves_nothing(world, migrated_engine):
    hr, people = _setup(world)
    ghost = [4, "NOPE999", "X", "Analyst", datetime(2026, 9, 1), 30000]
    odd = [5, people["odd"][0], "Y", "Chief Wizard", datetime(2026, 9, 1), 30000]
    r = _post(world, hr, "preview", _sheet(_rows(people, [ghost, odd])))
    assert r.status_code == 200, r.text
    by_row = {x["row"]: x for x in r.json()["rows"]}
    assert by_row[2]["status"] == "READY" and by_row[2]["designation"] == "Senior Analyst"
    assert by_row[2]["salaryAction"] == "PROPOSE" and by_row[2]["effectiveFrom"] == "2026-09-01"
    assert by_row[3]["status"] == "READY"
    assert by_row[3]["salaryAction"] == "PROPOSE" and by_row[3]["templatePending"] is True
    assert r.json()["summary"]["templatePending"] == 1
    assert by_row[5]["status"] == "ERROR" and "No employee" in by_row[5]["errors"][0]
    assert by_row[6]["status"] == "ERROR" and "listed designations" in by_row[6]["errors"][0]
    with migrated_engine.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM hr.salary_structure WHERE employee_id = CAST(:e AS uuid)"
                ),
                {"e": people["plain"][1]},
            ).scalar_one()
            == 0
        )


def test_commit_sets_designations_and_proposes_salaries_once(world, migrated_engine):
    hr, people = _setup(world)
    data = _sheet(_rows(people))
    r = _post(world, hr, "commit", data, rows="2,3,4")
    assert r.status_code == 200, r.text
    results = {x["row"]: x for x in r.json()["results"]}
    assert results[2]["designation"] == "UPDATED" and results[2]["salary"] == "PROPOSED"
    assert results[3]["designation"] == "UPDATED" and results[3]["salary"] == "PROPOSED"
    with migrated_engine.connect() as conn:
        plain = conn.execute(
            text(
                "SELECT status, gross_monthly, effective_from FROM hr.salary_structure"
                " WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": people["plain"][1]},
        ).all()
        band = conn.execute(
            text(
                "SELECT status, template_id, components::text FROM hr.salary_structure"
                " WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": people["band"][1]},
        ).all()
        audit = (
            conn.execute(
                text(
                    "SELECT changes::text FROM hr.audit_log"
                    " WHERE action IN ('SALARY_PROPOSED', 'DESIGNATIONS_AND_SALARIES_IMPORTED')"
                    " ORDER BY audit_id DESC LIMIT 6"
                )
            )
            .scalars()
            .all()
        )
    assert [(s, float(g), str(d)) for s, g, d in plain] == [("PROPOSED", 40000.0, "2026-09-01")]
    assert [(b[0], b[1], b[2]) for b in band] == [("PROPOSED", None, "[]")]  # template pending
    assert not any(
        "40000" in a or "60000" in a for a in audit
    )  # salaries stay out of the audit log
    emp = world.client.get(
        f"/hr/v1/employees/{people['plain'][1]}", headers=world.headers(hr)
    ).json()
    assert emp["designation"] == "Senior Analyst" and emp["salaryStatus"] == "WAITING_FINANCE"
    # running the same sheet again changes nothing
    again = _post(world, hr, "preview", data).json()["rows"]
    assert [x["status"] for x in again if x["row"] == 2] == ["NO_CHANGE"]
    repeat = _post(world, hr, "commit", data, rows="2").json()["results"][0]
    assert repeat["designation"] == "UNCHANGED" and repeat["salary"] == "EXISTS"
    with migrated_engine.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM hr.salary_structure WHERE employee_id = CAST(:e AS uuid)"
                ),
                {"e": people["plain"][1]},
            ).scalar_one()
            == 1
        )


def test_it_needs_both_the_employee_and_the_salary_permission(world):
    _, people = _setup(world)
    data = _sheet(_rows(people))
    only_employees = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    only_salary = world.grant(str(uuid.uuid4()), perm.HR_SALARY_PROPOSE)
    nobody = world.grant(str(uuid.uuid4()))
    for who in (only_employees, only_salary, nobody):
        assert _post(world, who, "preview", data).status_code == 403
        assert _post(world, who, "commit", data, rows="2").status_code == 403


def test_bad_files_and_too_many_rows_are_refused(world):
    hr, people = _setup(world)
    assert _post(world, hr, "preview", b"not excel").status_code == 422
    wb = Workbook()
    wb.active.append(["Name", "Phone"])
    out = io.BytesIO()
    wb.save(out)
    assert _post(world, hr, "preview", out.getvalue()).status_code == 422
    data = _sheet(_rows(people))
    assert (
        _post(world, hr, "commit", data, rows=",".join(str(i) for i in range(2, 14))).status_code
        == 422
    )
    assert _post(world, hr, "commit", data, rows="").status_code == 422


@pytest.fixture()
def mid_template_cleanup(migrated_engine):
    yield
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM hr.salary_structure WHERE template_id IN"
                " (SELECT template_id FROM hr.salary_template WHERE code = 'MID_21K_25K')"
            )
        )
        conn.execute(text("DELETE FROM hr.salary_template WHERE code = 'MID_21K_25K'"))


def test_a_template_pending_salary_cannot_be_approved_and_fills_when_the_middle_template_is_made(
    world, migrated_engine, mid_template_cleanup
):
    hr, people = _setup(world)
    finance = world.grant(str(uuid.uuid4()), perm.HR_SALARY_APPROVE, perm.HR_PAYROLL_READ)
    data = _sheet(_rows(people))
    _post(world, hr, "commit", data, rows="3")  # the 23,000 row
    emp = world.client.get(
        f"/hr/v1/employees/{people['band'][1]}", headers=world.headers(hr)
    ).json()
    assert emp["salaryStatus"] == "TEMPLATE_PENDING"
    listed = world.client.get(
        "/hr/v1/payroll/structures",
        params={"employee_id": people["band"][1]},
        headers=world.headers(finance),
    ).json()["items"]
    assert listed[0]["templatePending"] is True and listed[0]["components"] == []
    sid = listed[0]["structureId"]
    blocked = world.client.post(
        f"/hr/v1/payroll/structures/{sid}/decision",
        json={"decision": "APPROVE"},
        headers=world.headers(finance),
    )
    assert blocked.status_code == 409 and blocked.json()["code"] == "SALARY_TEMPLATE_PENDING"

    template = {
        "name": "Gross 21,001 to 24,999",
        "components": [
            {
                "code": "BASIC",
                "label": "Basic",
                "basis": "PERCENT_GROSS",
                "value": "50",
                "pf_wage": True,
            },
            {"code": "HRA", "label": "HRA", "basis": "PERCENT_BASIC", "value": "40"},
            {"code": "OTHER", "label": "Other allowance", "basis": "REMAINDER"},
        ],
    }
    made = world.client.post(
        "/hr/v1/payroll/templates?code=MID_21K_25K", json=template, headers=world.headers(hr)
    )
    assert made.status_code == 201, made.text
    filled = world.client.get(
        "/hr/v1/payroll/structures",
        params={"employee_id": people["band"][1]},
        headers=world.headers(finance),
    ).json()["items"][0]
    assert filled["templatePending"] is False
    assert sum(float(c["amount"]) for c in filled["components"]) == 23000.0
    approved = world.client.post(
        f"/hr/v1/payroll/structures/{sid}/decision",
        json={"decision": "APPROVE"},
        headers=world.headers(finance),
    )
    assert approved.status_code == 200 and approved.json()["status"] == "APPROVED"

    # with the template in place, a new salary in the band picks it without anyone choosing
    other, _ = world.employee(hr, date_of_joining="2026-09-01")
    auto = world.client.post(
        "/hr/v1/payroll/structures",
        json={
            "employee_id": other["employeeId"],
            "gross_monthly": "24999",
            "effective_from": "2026-09-01",
        },
        headers=world.headers(hr),
    )
    assert auto.status_code == 201 and auto.json()["templatePending"] is False
