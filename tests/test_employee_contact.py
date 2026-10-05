"""HR corrects an employee's email or mobile: the same Verigence login is updated first, and when it
cannot be, nothing changes in HR either."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from hrmgmt import permissions as perm
from tests.support import World, ist


@pytest.fixture()
def world(migrated_engine):
    return World(migrated_engine, now=ist(2026, 10, 12, 11, 0))


def hr_user(world):
    return world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE, perm.HR_EMPLOYEE_READ)


def mobile() -> str:
    return "9" + str(uuid.uuid4().int)[:9]


def patch(world, hr, emp, **body):
    return world.client.patch(
        f"/hr/v1/employees/{emp['employeeId']}", json=body, headers=world.headers(hr)
    )


def stored(world, emp):
    with world.engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT personal_email, mobile, security_user_id::text, login_status, login_error_code"
                " FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": emp["employeeId"]},
        ).one()


def actions(world, emp):
    with world.engine.connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                text("SELECT action FROM hr.audit_log WHERE entity_id = :e ORDER BY audit_id"),
                {"e": emp["employeeId"]},
            )
        ]


def prov(world):
    p = world.app.state.provisioner
    p.contact_calls = []
    p.contact_error = None
    return p


def test_the_email_is_changed_on_the_same_login_and_then_in_hr(world):
    hr = hr_user(world)
    emp, user_id = world.employee(hr)
    p = prov(world)
    new = f"new.{uuid.uuid4().hex[:8]}@example.com"
    r = patch(world, hr, emp, personal_email=new.upper())
    assert r.status_code == 200, r.text
    assert r.json()["personalEmail"] == new and r.json()["loginContactChanged"] is True
    assert p.contact_calls == [(user_id, new, None)]
    row = stored(world, emp)
    assert row[0] == new and row[2] == user_id  # same login, same user id
    assert "LOGIN_CONTACT_CHANGED" in actions(world, emp) and "EMPLOYEE_UPDATED" in actions(
        world, emp
    )


def test_the_mobile_is_changed_on_the_login_too(world):
    hr = hr_user(world)
    emp, user_id = world.employee(hr)
    p = prov(world)
    new = mobile()
    r = patch(world, hr, emp, mobile=new)
    assert r.status_code == 200 and r.json()["mobile"] == new
    assert p.contact_calls == [(user_id, None, new)]


def test_other_changes_do_not_touch_the_login(world):
    hr = hr_user(world)
    emp, _ = world.employee(hr)
    p = prov(world)
    r = patch(world, hr, emp, department="Finance")
    assert r.status_code == 200 and "loginContactChanged" not in r.json()
    assert p.contact_calls == []


@pytest.mark.parametrize(
    ("code", "status", "http"),
    [
        ("EMAIL_OR_MOBILE_EXISTS", "LOGIN_CONTACT_IN_USE", 409),
        ("CONTACT_NOT_VALID", "LOGIN_CONTACT_NOT_VALID", 422),
        ("SECURITY_UNAVAILABLE", None, 503),
        ("IDENTITY_PROVIDER_FAILED", None, 503),
    ],
)
def test_when_security_says_no_nothing_changes_in_hr(world, code, status, http):
    hr = hr_user(world)
    emp, _ = world.employee(hr)
    before = stored(world, emp)
    p = prov(world)
    p.contact_error = code
    r = patch(
        world, hr, emp, personal_email=f"x.{uuid.uuid4().hex[:8]}@example.com", mobile=mobile()
    )
    assert r.status_code == http, r.text
    if status:
        assert r.json()["code"] == status
    assert "Nothing was changed" in r.text or http == 422
    assert stored(world, emp) == before
    assert len(p.contact_calls) == 1  # one attempt, not repeated
    assert "LOGIN_CONTACT_CHANGED" not in actions(world, emp)


def test_an_email_or_mobile_of_another_employee_is_refused_before_security_is_asked(world):
    hr = hr_user(world)
    other, _ = world.employee(hr)
    emp, _ = world.employee(hr)
    p = prov(world)
    r = patch(world, hr, emp, personal_email=other["personalEmail"].upper())
    assert r.status_code == 409 and r.json()["code"] == "EMPLOYEE_EMAIL_EXISTS"
    r = patch(world, hr, emp, mobile=other["mobile"])
    assert r.status_code == 409 and r.json()["code"] == "EMPLOYEE_MOBILE_EXISTS"
    assert p.contact_calls == []


def test_the_same_values_again_change_nothing(world):
    hr = hr_user(world)
    emp, _ = world.employee(hr)
    p = prov(world)
    r = patch(world, hr, emp, personal_email=emp["personalEmail"], mobile=emp["mobile"])
    assert r.status_code == 200 and p.contact_calls == []


def test_a_login_needs_a_mobile_so_it_cannot_be_cleared(world):
    hr = hr_user(world)
    emp, _ = world.employee(hr)
    p = prov(world)
    r = patch(world, hr, emp, mobile=None)
    assert r.status_code == 409 and r.json()["code"] == "MOBILE_REQUIRED_FOR_LOGIN"
    assert stored(world, emp)[1] == emp["mobile"] and p.contact_calls == []


def test_without_a_configured_login_service_a_linked_employee_is_not_changed(world):
    hr = hr_user(world)
    emp, _ = world.employee(hr)
    before = stored(world, emp)
    saved = world.app.state.provisioner
    world.app.state.provisioner = None
    try:
        r = patch(world, hr, emp, personal_email=f"x.{uuid.uuid4().hex[:8]}@example.com")
    finally:
        world.app.state.provisioner = saved
    assert r.status_code == 503 and stored(world, emp) == before


def test_an_employee_without_a_login_changes_in_hr_only_and_may_try_a_login_again(world):
    hr = hr_user(world)
    emp, _ = world.employee(hr)
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.employee SET security_user_id = NULL, login_status = 'FAILED',"
                " login_error_code = 'EMAIL_OR_MOBILE_EXISTS' WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": emp["employeeId"]},
        )
    p = prov(world)
    new = mobile()
    r = patch(world, hr, emp, mobile=new)
    assert r.status_code == 200 and "loginContactChanged" not in r.json()
    row = stored(world, emp)
    assert row[1] == new and row[3] == "NOT_CREATED" and row[4] is None
    assert p.contact_calls == []


def test_a_new_employee_cannot_reuse_another_employees_mobile(world):
    hr = hr_user(world)
    other, _ = world.employee(hr)
    body = {
        "employee_code": f"W{uuid.uuid4().hex[:8]}".upper(),
        "full_name": "Test Person",
        "personal_email": f"t.{uuid.uuid4().hex[:8]}@example.com",
        "mobile": other["mobile"],
        "create_login": False,
    }
    r = world.client.post("/hr/v1/employees", json=body, headers=world.headers(hr))
    assert r.status_code == 409 and r.json()["code"] == "EMPLOYEE_MOBILE_EXISTS"


def test_only_hr_who_manage_employees_may_change_contact_details(world):
    hr = hr_user(world)
    emp, _ = world.employee(hr)
    reader = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_READ)
    p = prov(world)
    r = patch(world, reader, emp, mobile=mobile())
    assert r.status_code == 403 and p.contact_calls == []
