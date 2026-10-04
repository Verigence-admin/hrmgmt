from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from hrmgmt import permissions as perm
from hrmgmt.provisioning import ProvisioningError, UserSummary
from tests.support import World, ist


@pytest.fixture()
def world(migrated_engine):
    return World(migrated_engine, now=ist(2026, 10, 12, 11, 0))


def hr_user(world):
    return world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE, perm.HR_EMPLOYEE_READ)


def user(world, name="Person", email=None, status="ACTIVE", is_employee=False) -> UserSummary:
    uid = str(uuid.uuid4())
    u = UserSummary(uid, name, email or f"{uid[:8]}@example.com", status, is_employee)
    prov = world.app.state.provisioner
    prov.users = [*getattr(prov, "users", []), u]
    return u


def employee(world, hr, *, status="ACTIVE", linked_to: UserSummary | None = None, email=None):
    over = {"personal_email": email} if email else {}
    emp, _ = world.employee(hr, **over)
    sql = "UPDATE hr.employee SET employment_status = :s, security_user_id = CAST(:u AS uuid) WHERE employee_id = CAST(:e AS uuid)"
    with world.engine.begin() as conn:
        conn.execute(
            text(sql),
            {"s": status, "u": linked_to.user_id if linked_to else None, "e": emp["employeeId"]},
        )
    return emp


def sync(world, hr, apply=False):
    r = world.client.post(
        "/hr/v1/employees/sync-users", json={"apply": apply}, headers=world.headers(hr)
    )
    assert r.status_code == 200, r.text
    return r.json()


def item(body, emp):
    return next((i for i in body["items"] if i["employeeId"] == emp["employeeId"]), None)


def row(world, emp):
    with world.engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT security_user_id::text, login_status FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": emp["employeeId"]},
        ).one()


def test_a_linked_employee_ticks_the_user_and_a_preview_changes_nothing(world):
    hr = hr_user(world)
    u = user(world)
    emp = employee(world, hr, linked_to=u)
    preview = sync(world, hr)
    assert preview["applied"] is False and item(preview, emp)["tick"] is True
    assert not getattr(world.app.state.provisioner, "synced", [])
    done = sync(world, hr, apply=True)
    assert item(done, emp)["done"] == {
        "linked": False,
        "ticked": True,
        "suspended": False,
        "note": None,
    }
    assert world.app.state.provisioner.synced[-1] == [(u.user_id, False)]


def test_a_user_created_on_its_own_is_matched_by_email_and_linked(world):
    hr = hr_user(world)
    emp = employee(world, hr)
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.employee SET security_user_id = NULL, login_status = 'NOT_CREATED' WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": emp["employeeId"]},
        )
    u = user(world, email=emp["personalEmail"].upper())
    plan = item(sync(world, hr), emp)
    assert plan["link"] is True and plan["tick"] is True and plan["userId"] == u.user_id
    assert row(world, emp)[0] is None  # a preview links nothing
    done = item(sync(world, hr, apply=True), emp)
    assert done["done"]["linked"] is True and done["done"]["ticked"] is True
    assert row(world, emp) == (u.user_id, "CREATED")
    again = sync(world, hr, apply=True)
    assert item(again, emp) is None  # nothing left to do for this person


@pytest.mark.parametrize("status", ["INACTIVE", "EXITED"])
def test_an_employee_who_is_not_active_suspends_an_active_user(world, status):
    hr = hr_user(world)
    u = user(world, is_employee=True)
    emp = employee(world, hr, status=status, linked_to=u)
    plan = item(sync(world, hr), emp)
    assert plan["suspend"] is True and plan["tick"] is False
    done = item(sync(world, hr, apply=True), emp)
    assert done["done"]["suspended"] is True
    assert world.app.state.provisioner.synced[-1] == [(u.user_id, True)]


def test_nothing_is_reactivated_and_pending_users_are_only_reported(world):
    hr = hr_user(world)
    suspended = user(world, status="SUSPENDED", is_employee=True)
    pending = user(world, status="PENDING", is_employee=True)
    pending_gone = user(world, status="PENDING", is_employee=True)
    active_emp = employee(world, hr, linked_to=suspended)
    pending_emp = employee(world, hr, linked_to=pending)
    gone_emp = employee(world, hr, status="EXITED", linked_to=pending_gone)
    body = sync(world, hr, apply=True)
    assert item(body, active_emp)["attention"] == ["EMPLOYEE_ACTIVE_USER_SUSPENDED"]
    assert item(body, pending_emp)["attention"] == ["USER_PENDING_APPROVAL"]
    assert item(body, gone_emp)["attention"] == ["EMPLOYEE_NOT_ACTIVE_USER_PENDING"]
    assert not any(
        i["suspend"]
        for i in (item(body, active_emp), item(body, pending_emp), item(body, gone_emp))
    )
    assert body["summary"]["needAttention"] >= 3


def test_employees_without_a_login_or_with_a_missing_or_shared_one_are_reported(world):
    hr = hr_user(world)
    lonely = employee(world, hr)
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.employee SET security_user_id = NULL WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": lonely["employeeId"]},
        )
    dangling = employee(
        world,
        hr,
        linked_to=UserSummary(str(uuid.uuid4()), "Gone", "gone@example.com", "ACTIVE", False),
    )
    owner_user = user(world)
    owner = employee(world, hr, linked_to=owner_user)
    sharer = employee(world, hr)
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.employee SET security_user_id = NULL, secondary_email = :m WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"m": owner_user.email, "e": sharer["employeeId"]},
        )
    body = sync(world, hr)
    reasons = {u["employeeId"]: u["reason"] for u in body["unmatched"]}
    assert reasons[lonely["employeeId"]] == "NO_LOGIN"
    assert reasons[dangling["employeeId"]] == "LINKED_USER_MISSING"
    assert reasons[sharer["employeeId"]] == "LOGIN_IN_USE"
    assert owner["employeeId"] not in reasons


def test_users_that_are_not_employees_are_left_alone(world):
    hr = hr_user(world)
    outsider = user(world, name="Outside Person")
    body = sync(world, hr, apply=True)
    assert all(i["userId"] != outsider.user_id for i in body["items"])
    assert all(
        outsider.user_id not in [uid for uid, _ in call]
        for call in getattr(world.app.state.provisioner, "synced", [])
    )


def test_a_failure_in_security_changes_nothing_here_and_a_permission_is_needed(world):
    hr = hr_user(world)
    emp = employee(world, hr)
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.employee SET security_user_id = NULL WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": emp["employeeId"]},
        )
    user(world, email=emp["personalEmail"])
    prov = world.app.state.provisioner

    def boom(*, items):
        raise ProvisioningError("SECURITY_UNAVAILABLE", "x")

    prov.sync_employees = boom
    r = world.client.post(
        "/hr/v1/employees/sync-users", json={"apply": True}, headers=world.headers(hr)
    )
    assert r.status_code == 503
    assert row(world, emp)[0] is None  # the link was not saved
    nobody = world.grant(str(uuid.uuid4()))
    assert (
        world.client.post(
            "/hr/v1/employees/sync-users", json={}, headers=world.headers(nobody)
        ).status_code
        == 403
    )
