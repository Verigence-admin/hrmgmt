"""What an employee may edit about themselves, and how a new email or mobile reaches HR."""

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


def mine(world, user, **body):
    return world.client.patch("/hr/v1/me/employee", json=body, headers=world.headers(user))


def ask(world, user, field, value):
    return world.client.post(
        "/hr/v1/me/employee/contact-change",
        json={"field": field, "new_value": value},
        headers=world.headers(user),
    )


def decide(world, who, change, action, **body):
    return world.client.post(
        f"/hr/v1/employee-contact-changes/{change['changeId']}/{action}",
        json=body,
        headers=world.headers(who),
    )


def row(world, emp):
    with world.engine.connect() as conn:
        return (
            conn.execute(
                text(
                    "SELECT full_name, gender, personal_email, mobile, address, state, district,"
                    " pincode, emergency_contact_name, emergency_contact_number,"
                    " emergency_contact_address, secondary_email, security_user_id::text AS uid,"
                    " designation_code, department FROM hr.employee"
                    " WHERE employee_id = CAST(:e AS uuid)"
                ),
                {"e": emp["employeeId"]},
            )
            .mappings()
            .one()
        )


def audit(world, emp):
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


# ---- what an employee may edit ---------------------------------------------------------------


def test_an_employee_can_edit_their_own_name_gender_address_and_emergency_contact(world):
    hr = hr_user(world)
    emp, user = world.employee(hr)
    new_number = mobile()
    r = mine(
        world,
        user,
        full_name="  Asha   Rao Nair ",
        gender="FEMALE",
        address="12 Station Road",
        state="odisha",
        district="Cuttack",
        pincode="753001",
        emergency_contact_name="Ravi Rao",
        emergency_contact_number=f"+91 {new_number}",
        emergency_contact_address="Bhubaneswar",
        secondary_email="Asha.Backup@Example.com",
    )
    assert r.status_code == 200, r.text
    seen = row(world, emp)
    assert seen["full_name"] == "Asha Rao Nair" and seen["gender"] == "FEMALE"
    assert seen["emergency_contact_name"] == "Ravi Rao"
    assert seen["emergency_contact_number"] == new_number
    assert seen["secondary_email"] == "asha.backup@example.com"
    assert seen["district"] == "Cuttack" and seen["pincode"] == "753001"
    assert "EMPLOYEE_SELF_UPDATED" in audit(world, emp)


def test_the_emergency_contact_alone_can_be_changed(world):
    hr = hr_user(world)
    emp, user = world.employee(hr)
    number = mobile()
    r = mine(world, user, emergency_contact_name="Meena Rao", emergency_contact_number=number)
    assert r.status_code == 200
    assert (
        row(world, emp)["emergency_contact_name"],
        row(world, emp)["emergency_contact_number"],
    ) == (
        "Meena Rao",
        number,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("designation_code", "MANAGER"),
        ("department", "Finance"),
        ("mobile", "9000000000"),
        ("personal_email", "x@y.com"),
        ("date_of_birth", "1990-01-01"),
        ("date_of_joining", "2020-01-01"),
        ("employee_code", "ZZ1"),
        ("total_experience_years", 9),
        ("pan", "ABCDE1234F"),
        ("aadhaar", "123456789012"),
        ("employment_status", "QUIT"),
        ("role", "CEO"),
    ],
)
def test_the_fields_kept_by_hr_cannot_be_changed_by_the_employee(world, field, value):
    hr = hr_user(world)
    emp, user = world.employee(hr)
    before = row(world, emp)
    r = mine(world, user, **{field: value})
    assert r.status_code == 422
    assert row(world, emp) == before


def test_the_name_cannot_be_made_empty(world):
    hr = hr_user(world)
    emp, user = world.employee(hr)
    before = row(world, emp)["full_name"]
    assert mine(world, user, full_name="   ").status_code == 422
    assert row(world, emp)["full_name"] == before


# ---- asking HR to change the email or mobile -------------------------------------------------


def test_an_employee_asks_and_nothing_changes_until_hr_approves(world):
    hr = hr_user(world)
    emp, user = world.employee(hr)
    old_email, old_mobile = emp["personalEmail"], emp["mobile"]
    new_email = f"new.{uuid.uuid4().hex[:8]}@example.com"
    r = ask(world, user, "EMAIL", new_email.upper())
    assert r.status_code == 201, r.text
    change = r.json()
    assert change["status"] == "PENDING" and change["field"] == "EMAIL"
    assert change["oldValue"] == old_email and change["newValue"] == new_email
    new_mobile = mobile()
    second = ask(world, user, "MOBILE", f"+91 {new_mobile[:5]} {new_mobile[5:]}").json()
    assert second["oldValue"] == old_mobile and second["newValue"] == new_mobile
    seen = row(world, emp)
    assert seen["personal_email"] == old_email and seen["mobile"] == old_mobile
    assert "CONTACT_CHANGE_REQUESTED" in audit(world, emp)


def test_a_bad_same_taken_or_repeated_request_is_refused(world):
    hr = hr_user(world)
    other, _ = world.employee(hr)
    emp, user = world.employee(hr)
    assert ask(world, user, "EMAIL", "not-an-email").status_code == 422
    assert ask(world, user, "MOBILE", "12345").status_code == 422
    assert (
        ask(world, user, "EMAIL", emp["personalEmail"].upper()).json()["code"]
        == "CONTACT_UNCHANGED"
    )
    assert ask(world, user, "EMAIL", other["personalEmail"]).json()["code"] == "CONTACT_IN_USE"
    assert ask(world, user, "MOBILE", other["mobile"]).json()["code"] == "CONTACT_IN_USE"
    assert ask(world, user, "MOBILE", mobile()).status_code == 201
    again = ask(world, user, "MOBILE", mobile())
    assert again.status_code == 409 and again.json()["code"] == "CONTACT_CHANGE_PENDING"


def test_an_employee_sees_and_cancels_only_their_own_requests(world):
    hr = hr_user(world)
    emp, user = world.employee(hr)
    _, someone = world.employee(hr)
    change = ask(world, user, "MOBILE", mobile()).json()
    listed = world.client.get(
        "/hr/v1/me/employee/contact-changes", headers=world.headers(user)
    ).json()
    assert [c["changeId"] for c in listed["items"]] == [change["changeId"]]
    other_view = world.client.get(
        "/hr/v1/me/employee/contact-changes", headers=world.headers(someone)
    ).json()
    assert other_view["items"] == []
    url = f"/hr/v1/me/employee/contact-changes/{change['changeId']}/cancel"
    assert world.client.post(url, headers=world.headers(someone)).status_code == 404
    done = world.client.post(url, headers=world.headers(user))
    assert done.status_code == 200 and done.json()["status"] == "CANCELLED"
    assert ask(world, user, "MOBILE", mobile()).status_code == 201  # a new one is allowed


# ---- HR decides ------------------------------------------------------------------------------


def test_hr_approves_an_email_change_and_the_login_changes_first(world):
    hr = hr_user(world)
    emp, user = world.employee(hr)
    p = prov(world)
    new_email = f"new.{uuid.uuid4().hex[:8]}@example.com"
    change = ask(world, user, "EMAIL", new_email).json()
    done = decide(world, hr, change, "approve", note="Checked")
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "APPROVED" and body["loginOutcome"] == "LOGIN_UPDATED"
    assert row(world, emp)["personal_email"] == new_email
    assert p.contact_calls == [(row(world, emp)["uid"], new_email, None)]
    assert "CONTACT_CHANGE_APPROVED" in audit(world, emp)


def test_hr_approves_a_mobile_change_for_someone_without_a_login(world):
    hr = hr_user(world)
    emp, user = world.employee(hr)
    new_mobile = mobile()
    change = ask(world, user, "MOBILE", new_mobile).json()
    with world.engine.begin() as conn:  # the login is unlinked after the employee asked
        conn.execute(
            text(
                "UPDATE hr.employee SET security_user_id = NULL WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": emp["employeeId"]},
        )
    p = prov(world)
    done = decide(world, hr, change, "approve").json()
    assert done["status"] == "APPROVED" and done["loginOutcome"] == "NO_LOGIN"
    assert row(world, emp)["mobile"] == new_mobile and p.contact_calls == []


def test_if_security_refuses_nothing_changes_and_the_request_stays_open(world):
    hr = hr_user(world)
    emp, user = world.employee(hr)
    p = prov(world)
    p.contact_error = "SECURITY_UNAVAILABLE"
    before = row(world, emp)
    change = ask(world, user, "MOBILE", mobile()).json()
    r = decide(world, hr, change, "approve")
    assert r.status_code == 503 and "Nothing was changed" in r.text
    assert row(world, emp) == before
    p.contact_error = None
    assert decide(world, hr, change, "approve").json()["status"] == "APPROVED"  # HR tries again


def test_hr_cannot_approve_their_own_request_and_a_reader_cannot_decide(world):
    hr = hr_user(world)
    emp, user = world.employee(hr)
    change = ask(world, user, "MOBILE", mobile()).json()
    # the same person is both the employee and an HR user
    both = world.grant(user, perm.HR_EMPLOYEE_MANAGE, perm.HR_EMPLOYEE_READ)
    assert decide(world, both, change, "approve").status_code == 403
    reader = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_READ)
    assert decide(world, reader, change, "approve").status_code == 403
    assert decide(world, reader, change, "reject", note="no").status_code == 403
    listed = world.client.get(
        "/hr/v1/employee-contact-changes",
        params={"status": "PENDING"},
        headers=world.headers(reader),
    )
    assert listed.status_code == 200 and change["changeId"] in [
        c["changeId"] for c in listed.json()["items"]
    ]


def test_a_rejection_needs_a_reason_and_leaves_the_record(world):
    hr = hr_user(world)
    emp, user = world.employee(hr)
    before = row(world, emp)
    change = ask(world, user, "EMAIL", f"new.{uuid.uuid4().hex[:8]}@example.com").json()
    assert decide(world, hr, change, "reject").status_code == 422
    done = decide(world, hr, change, "reject", note="Use your company id")
    assert done.status_code == 200 and done.json()["status"] == "REJECTED"
    assert row(world, emp) == before
    assert decide(world, hr, change, "approve").status_code == 409  # decided already


def test_an_approval_is_refused_when_the_record_changed_in_the_meantime(world):
    hr = hr_user(world)
    emp, user = world.employee(hr)
    change = ask(world, user, "MOBILE", mobile()).json()
    prov(world)
    r = world.client.patch(
        f"/hr/v1/employees/{emp['employeeId']}",
        json={"mobile": mobile()},
        headers=world.headers(hr),
    )
    assert r.status_code == 200
    out = decide(world, hr, change, "approve")
    assert out.status_code == 409 and out.json()["code"] == "CONTACT_CHANGED_MEANWHILE"


def test_an_email_taken_by_someone_else_meanwhile_is_refused_on_approval(world):
    hr = hr_user(world)
    emp, user = world.employee(hr)
    other, _ = world.employee(hr)
    target = f"late.{uuid.uuid4().hex[:8]}@example.com"
    change = ask(world, user, "EMAIL", target).json()
    world.client.patch(
        f"/hr/v1/employees/{other['employeeId']}",
        json={"personal_email": target},
        headers=world.headers(hr),
    )
    prov(world)
    out = decide(world, hr, change, "approve")
    assert out.status_code == 409 and out.json()["code"] == "EMPLOYEE_EMAIL_EXISTS"
    assert row(world, emp)["personal_email"] == emp["personalEmail"]


def test_requests_go_with_the_employee_when_they_are_deleted(world):
    hr = hr_user(world)
    emp, user = world.employee(hr)
    ask(world, user, "MOBILE", mobile())
    keeper = world.grant(str(uuid.uuid4()), perm.HR_HOUSEKEEPING_MANAGE)
    r = world.client.post(
        "/hr/v1/housekeeping/employee/delete",
        json={"employee_code": emp["employeeCode"], "confirm": "DELETE"},
        headers=world.headers(keeper),
    )
    assert r.status_code == 200, r.text
