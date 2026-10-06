"""An employee's status changes when HR asks and the CEO approves; HR's suspension (dated today or earlier) is immediate."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from hrmgmt import permissions as perm
from hrmgmt.provisioning import ProvisioningError, UserSummary
from tests.support import World, ist

NOW = ist(2026, 10, 12, 11, 0)


@pytest.fixture()
def world(migrated_engine):
    return World(migrated_engine, now=NOW)


def hr_user(world):
    return world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE, perm.HR_EMPLOYEE_READ)


def ceo_user(world):
    return world.grant(
        str(uuid.uuid4()),
        perm.HR_EMPLOYEE_MANAGE,
        perm.HR_EMPLOYEE_READ,
        perm.HR_EMPLOYEE_STATUS_APPROVE,
    )


def ask(world, hr, emp, to="TERMINATED", **over):
    body = {"to_status": to, "reason": "Absent without notice", **over}
    return world.client.post(
        f"/hr/v1/employees/{emp['employeeId']}/status-change", json=body, headers=world.headers(hr)
    )


def decide(world, who, change, action, **body):
    return world.client.post(
        f"/hr/v1/employee-status-changes/{change['changeId']}/{action}",
        json=body,
        headers=world.headers(who),
    )


def status_of(world, emp):
    with world.engine.connect() as conn:
        return conn.execute(
            text("SELECT employment_status FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"),
            {"e": emp["employeeId"]},
        ).scalar_one()


def actions(world, emp):
    with world.engine.connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                text("SELECT action FROM hr.audit_log WHERE entity_id = :e ORDER BY audit_id"),
                {"e": emp["employeeId"]},
            )
        ]


def linked(world, hr, login_status="ACTIVE"):
    """An employee whose Verigence login exists in the fake Security."""
    emp, user_id = world.employee(hr)
    prov = world.app.state.provisioner
    prov.users = [
        *getattr(prov, "users", []),
        UserSummary(user_id, "Test Person", emp["personalEmail"], login_status, True),
    ]
    prov.synced = []
    return emp, user_id


def test_asking_changes_nothing_until_the_ceo_approves(world):
    hr = hr_user(world)
    emp, _ = world.employee(hr)
    r = ask(world, hr, emp, "TERMINATED", effective_date="2026-10-12")
    assert r.status_code == 201, r.text
    change = r.json()
    assert change["status"] == "PENDING" and change["fromStatus"] == "ACTIVE"
    assert change["toStatus"] == "TERMINATED" and change["requestedBy"] == hr
    assert status_of(world, emp) == "ACTIVE"
    assert "STATUS_CHANGE_REQUESTED" in actions(world, emp)


def test_the_ceo_approves_and_the_status_changes(world):
    hr, ceo = hr_user(world), ceo_user(world)
    emp, user_id = linked(world, hr)
    change = ask(world, hr, emp, "QUIT").json()
    done = decide(world, ceo, change, "approve", note="Resignation accepted")
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "APPROVED" and body["decidedBy"] == ceo
    assert body["decisionNote"] == "Resignation accepted"
    assert status_of(world, emp) == "QUIT"
    assert body["loginOutcome"] == "LOGIN_SUSPENDED"
    assert world.app.state.provisioner.synced == [[(user_id, True)]]
    assert "EMPLOYEE_STATUS_CHANGED" in actions(world, emp)


def test_the_person_who_asked_cannot_approve_their_own_request(world):
    ceo = ceo_user(world)
    emp, _ = world.employee(ceo)
    change = ask(world, ceo, emp).json()
    r = decide(world, ceo, change, "approve")
    assert r.status_code == 403
    assert status_of(world, emp) == "ACTIVE"


def test_hr_alone_cannot_approve_or_reject(world):
    hr = hr_user(world)
    emp, _ = world.employee(hr)
    change = ask(world, hr, emp).json()
    assert decide(world, hr, change, "approve").status_code == 403
    assert decide(world, hr, change, "reject", note="no").status_code == 403
    assert status_of(world, emp) == "ACTIVE"


def test_only_hr_who_manage_employees_can_ask(world):
    hr = hr_user(world)
    emp, _ = world.employee(hr)
    reader = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_READ)
    assert ask(world, reader, emp).status_code == 403


def test_only_one_open_request_per_employee(world):
    hr = hr_user(world)
    emp, _ = world.employee(hr)
    assert ask(world, hr, emp).status_code == 201
    again = ask(world, hr, emp, "QUIT")
    assert again.status_code == 409 and again.json()["code"] == "STATUS_CHANGE_PENDING"


@pytest.mark.parametrize(
    ("over", "http"),
    [
        ({"to": "ACTIVE"}, 409),  # already active
        ({"to": "INACTIVE"}, 422),  # not a status any more
        ({"to": "EXITED"}, 422),
        ({"reason": "x"}, 422),
        ({"effective_date": "2020-01-01"}, 422),
        ({"effective_date": "2030-01-01"}, 422),
    ],
)
def test_bad_requests_are_refused(world, over, http):
    hr = hr_user(world)
    emp, _ = world.employee(hr)
    assert ask(world, hr, emp, **over).status_code == http
    assert status_of(world, emp) == "ACTIVE"


def test_a_rejected_request_leaves_the_status_and_needs_a_reason(world):
    hr, ceo = hr_user(world), ceo_user(world)
    emp, _ = world.employee(hr)
    change = ask(world, hr, emp).json()
    assert decide(world, ceo, change, "reject").status_code == 422
    r = decide(world, ceo, change, "reject", note="Not agreed")
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"
    assert status_of(world, emp) == "ACTIVE"
    assert ask(world, hr, emp, "QUIT").status_code == 201  # a new request is allowed now


def test_only_the_person_who_asked_can_cancel_and_a_decided_request_is_final(world):
    hr, other, ceo = hr_user(world), hr_user(world), ceo_user(world)
    emp, _ = world.employee(hr)
    change = ask(world, hr, emp).json()
    assert decide(world, other, change, "cancel").status_code == 403
    assert decide(world, hr, change, "cancel").json()["status"] == "CANCELLED"
    assert decide(world, ceo, change, "approve").status_code == 409
    assert status_of(world, emp) == "ACTIVE"


def test_an_approval_is_refused_when_the_status_moved_in_the_meantime(world):
    hr, ceo = hr_user(world), ceo_user(world)
    emp, _ = world.employee(hr)
    change = ask(world, hr, emp, "QUIT").json()
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.employee SET employment_status = 'SUSPENDED'"
                " WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": emp["employeeId"]},
        )
    r = decide(world, ceo, change, "approve")
    assert r.status_code == 409 and r.json()["code"] == "STATUS_CHANGED_MEANWHILE"
    assert status_of(world, emp) == "SUSPENDED"


def test_if_security_cannot_suspend_the_login_the_status_still_changes_and_says_so(world):
    hr, ceo = hr_user(world), ceo_user(world)
    emp, _ = linked(world, hr)
    change = ask(world, hr, emp, "TERMINATED").json()

    def down(*, items):
        raise ProvisioningError("SECURITY_UNAVAILABLE", "x")

    world.app.state.provisioner.sync_employees = down
    r = decide(world, ceo, change, "approve")
    assert r.status_code == 200 and r.json()["loginOutcome"] == "LOGIN_NOT_UPDATED"
    assert status_of(world, emp) == "TERMINATED"


def test_an_employee_without_a_login_has_nothing_to_suspend(world):
    hr, ceo = hr_user(world), ceo_user(world)
    emp, _ = world.employee(hr)
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.employee SET security_user_id = NULL WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": emp["employeeId"]},
        )
    change = ask(world, hr, emp).json()
    assert decide(world, ceo, change, "approve").json()["loginOutcome"] == "NO_LOGIN"


def test_coming_back_to_active_never_reactivates_the_login(world):
    hr, ceo = hr_user(world), ceo_user(world)
    emp, _ = linked(world, hr, login_status="SUSPENDED")
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.employee SET employment_status = 'SUSPENDED'"
                " WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": emp["employeeId"]},
        )
    change = ask(world, hr, emp, "ACTIVE").json()
    done = decide(world, ceo, change, "approve").json()
    assert status_of(world, emp) == "ACTIVE"
    assert done["loginOutcome"] == "LOGIN_NEEDS_SUPERADMIN"
    assert world.app.state.provisioner.synced == []  # nothing was sent that could change the login


def test_the_status_cannot_be_edited_directly_any_more(world):
    hr = hr_user(world)
    emp, _ = world.employee(hr)
    r = world.client.patch(
        f"/hr/v1/employees/{emp['employeeId']}",
        json={"employment_status": "QUIT"},
        headers=world.headers(hr),
    )
    assert r.status_code == 422 and status_of(world, emp) == "ACTIVE"


def test_the_lists_the_history_and_the_counts(world):
    hr, ceo = hr_user(world), ceo_user(world)
    emp, _ = world.employee(hr)
    before = world.client.get("/hr/v1/employees/summary", headers=world.headers(hr)).json()
    change = ask(world, hr, emp, "TERMINATED").json()
    pending = world.client.get(
        "/hr/v1/employee-status-changes", params={"status": "PENDING"}, headers=world.headers(ceo)
    ).json()["items"]
    assert change["changeId"] in [c["changeId"] for c in pending]
    decide(world, ceo, change, "approve")
    history = world.client.get(
        f"/hr/v1/employees/{emp['employeeId']}/status-changes", headers=world.headers(hr)
    ).json()["items"]
    assert [h["status"] for h in history] == ["APPROVED"]
    after = world.client.get("/hr/v1/employees/summary", headers=world.headers(hr)).json()
    assert (
        after["terminated"] == before["terminated"] + 1 and after["active"] == before["active"] - 1
    )
    listed = world.client.get(
        "/hr/v1/employees",
        params={"status": "TERMINATED", "q": emp["employeeCode"]},
        headers=world.headers(hr),
    ).json()
    assert [e["employeeCode"] for e in listed["items"]] == [emp["employeeCode"]]


def test_hr_admin_never_changes_a_status_at_once_not_even_a_suspension(world):
    hr, ceo = hr_user(world), ceo_user(world)
    emp, _ = linked(world, hr)
    for to in ("SUSPENDED", "TERMINATED", "QUIT"):
        r = ask(world, hr, emp, to, effective_date="2026-10-12")
        assert r.status_code == 201 and r.json()["status"] == "PENDING", (to, r.text)
        assert status_of(world, emp) == "ACTIVE"
        assert (
            decide(world, hr, r.json(), "cancel").status_code == 200
        )  # take it back to ask the next
    change = ask(world, hr, emp, "SUSPENDED").json()
    assert decide(world, ceo, change, "approve").status_code == 200
    assert status_of(world, emp) == "SUSPENDED"


def super_user(world):
    return world.grant(
        str(uuid.uuid4()),
        perm.HR_EMPLOYEE_MANAGE,
        perm.HR_EMPLOYEE_READ,
        perm.HR_EMPLOYEE_STATUS_DIRECT,
    )


def test_super_admin_sets_any_status_at_once(world):
    boss = super_user(world)
    emp, user_id = linked(world, boss)
    r = ask(world, boss, emp, "QUIT", effective_date="2026-10-10")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "APPROVED" and body["toStatus"] == "QUIT" and body["decidedBy"] == boss
    assert body["decisionNote"] == "Applied directly" and body["loginOutcome"] == "LOGIN_SUSPENDED"
    assert status_of(world, emp) == "QUIT"
    assert world.app.state.provisioner.synced == [[(user_id, True)]]
    done = actions(world, emp)
    assert "EMPLOYEE_STATUS_CHANGED" in done and "STATUS_CHANGE_REQUESTED" not in done


def test_super_admin_can_bring_someone_back_but_the_login_is_never_reactivated(world):
    boss = super_user(world)
    emp, _ = linked(world, boss, login_status="SUSPENDED")
    assert ask(world, boss, emp, "TERMINATED").json()["status"] == "APPROVED"
    back = ask(world, boss, emp, "ACTIVE")
    assert back.status_code == 201 and back.json()["status"] == "APPROVED"
    assert status_of(world, emp) == "ACTIVE"
    assert back.json()["loginOutcome"] == "LOGIN_NEEDS_SUPERADMIN"


def test_a_future_dated_change_by_super_admin_still_waits_and_hr_alone_cannot_do_it_directly(world):
    boss, hr = super_user(world), hr_user(world)
    later, _ = world.employee(boss)
    assert (
        ask(world, boss, later, "TERMINATED", effective_date="2026-10-20").json()["status"]
        == "PENDING"
    )
    assert status_of(world, later) == "ACTIVE"
    other, _ = world.employee(hr)
    assert ask(world, hr, other, "QUIT", effective_date="2026-10-12").json()["status"] == "PENDING"
    assert status_of(world, other) == "ACTIVE"


def test_the_ceo_role_holding_the_direct_permission_changes_a_status_at_once(world):
    ceo = world.grant(
        str(uuid.uuid4()),
        perm.HR_EMPLOYEE_MANAGE,
        perm.HR_EMPLOYEE_READ,
        perm.HR_EMPLOYEE_STATUS_APPROVE,
        perm.HR_EMPLOYEE_STATUS_DIRECT,
    )
    emp, _ = world.employee(ceo)
    r = ask(world, ceo, emp, "TERMINATED")
    assert (
        r.status_code == 201
        and r.json()["status"] == "APPROVED"
        and status_of(world, emp) == "TERMINATED"
    )


def test_an_immediate_change_still_stands_when_security_cannot_suspend_the_login(world):
    boss = super_user(world)
    emp, _ = linked(world, boss)

    def down(*, items):
        raise ProvisioningError("SECURITY_UNAVAILABLE", "x")

    world.app.state.provisioner.sync_employees = down
    r = ask(world, boss, emp, "QUIT")
    assert r.status_code == 201 and r.json()["loginOutcome"] == "LOGIN_NOT_UPDATED"
    assert status_of(world, emp) == "QUIT"
