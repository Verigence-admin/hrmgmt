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


# ---- creating the logins that are missing --------------------------------------------------


def _mobile() -> str:
    return "9" + str(uuid.uuid4().int)[:9]


class Creator:
    """Stands in for Security's create call and records every attempt."""

    def __init__(self, world, fail_with: str | None = None):
        self.calls: list[dict] = []
        self.fail_with = fail_with
        prov = world.app.state.provisioner

        def create_user(**kwargs):
            self.calls.append(kwargs)
            if self.fail_with:
                raise ProvisioningError(self.fail_with, "x")
            from hrmgmt.provisioning import CreatedLogin

            uid = str(uuid.uuid4())
            prov.users = [
                *getattr(prov, "users", []),
                UserSummary(uid, kwargs["first_name"], kwargs["email"], "ACTIVE", True),
            ]
            return CreatedLogin(user_id=uid)

        prov.create_user = create_user


def no_login_employee(world, hr, **over):
    over.setdefault("mobile", _mobile())
    emp, _ = world.employee(hr, **over)
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.employee SET security_user_id = NULL, login_status = 'NOT_CREATED'"
                " WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": emp["employeeId"]},
        )
    return emp


def create(world, hr, *emps):
    r = world.client.post(
        "/hr/v1/employees/sync-users/create",
        json={"employeeIds": [e["employeeId"] for e in emps]},
        headers=world.headers(hr),
    )
    assert r.status_code == 200, r.text
    return {x["employeeId"]: x for x in r.json()["results"]}


def unmatched(body, emp):
    return next((u for u in body["unmatched"] if u["employeeId"] == emp["employeeId"]), None)


def test_the_check_says_who_can_get_a_login_and_who_must_be_fixed_first(world):
    hr = hr_user(world)
    ready = no_login_employee(world, hr)
    no_mobile = no_login_employee(world, hr)
    shared_a = no_login_employee(world, hr, mobile="9123456780")
    no_login_employee(world, hr, mobile="9123456780")
    left = no_login_employee(world, hr)
    refused = no_login_employee(world, hr)
    with world.engine.begin() as conn:
        conn.execute(
            text("UPDATE hr.employee SET mobile = NULL WHERE employee_id = CAST(:e AS uuid)"),
            {"e": no_mobile["employeeId"]},
        )
        conn.execute(
            text(
                "UPDATE hr.employee SET employment_status = 'EXITED' WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": left["employeeId"]},
        )
        conn.execute(
            text(
                "UPDATE hr.employee SET login_status = 'FAILED', login_error_code = 'EMAIL_OR_MOBILE_EXISTS'"
                " WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": refused["employeeId"]},
        )
    body = sync(world, hr)
    assert unmatched(body, ready)["canCreate"] is True
    assert unmatched(body, ready)["email"] == ready["personalEmail"]
    assert unmatched(body, no_mobile)["blocked"] == "NO_MOBILE"
    assert unmatched(body, shared_a)["blocked"] == "MOBILE_SHARED"
    assert unmatched(body, left)["blocked"] == "EMPLOYEE_NOT_ACTIVE"
    assert unmatched(body, refused)["blocked"] == "EMAIL_OR_MOBILE_EXISTS"
    assert body["summary"]["toCreate"] >= 1
    assert not getattr(world.app.state.provisioner, "synced", [])  # the check changes nothing


def test_a_login_is_created_active_linked_and_the_password_is_never_returned(world):
    hr = hr_user(world)
    creator = Creator(world)
    emp = no_login_employee(world, hr, full_name="Asha Rao Nair")
    creator.calls.clear()  # setting up the employee also asked for a login
    out = create(world, hr, emp)[emp["employeeId"]]
    assert out["outcome"] == "CREATED" and out["reason"] is None
    assert len(creator.calls) == 1
    call = creator.calls[0]
    assert (call["first_name"], call["last_name"]) == ("Asha", "Rao Nair")
    assert call["email"] == emp["personalEmail"] and call["mobile"] == emp["mobile"]
    assert len(call["password"]) >= 8
    assert row(world, emp)[1] == "CREATED" and row(world, emp)[0] is not None
    raw = world.client.post(
        "/hr/v1/employees/sync-users/create",
        json={"employeeIds": [emp["employeeId"]]},
        headers=world.headers(hr),
    ).text
    assert call["password"] not in raw
    with world.engine.connect() as conn:
        audit = conn.execute(
            text(
                "SELECT changes::text FROM hr.audit_log WHERE entity_id = :e AND action = 'LOGIN_CREATED'"
            ),
            {"e": emp["employeeId"]},
        ).all()
    assert len(audit) >= 2  # one when the employee was added, one from this sync
    assert all(call["password"] not in a[0] for a in audit)


def test_a_second_press_creates_nothing_again(world):
    hr = hr_user(world)
    creator = Creator(world)
    emp = no_login_employee(world, hr)
    creator.calls.clear()
    assert create(world, hr, emp)[emp["employeeId"]]["outcome"] == "CREATED"
    again = create(world, hr, emp)[emp["employeeId"]]
    assert again["outcome"] == "SKIPPED" and again["reason"] == "ALREADY_LINKED"
    assert len(creator.calls) == 1


def test_nothing_is_created_when_the_employee_cannot_have_a_login_or_one_exists(world):
    hr = hr_user(world)
    creator = Creator(world)
    left = no_login_employee(world, hr)
    no_mobile = no_login_employee(world, hr)
    a = no_login_employee(world, hr, mobile="9123456781")
    b = no_login_employee(world, hr, mobile="9123456781")
    known = no_login_employee(world, hr)
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.employee SET employment_status = 'INACTIVE' WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": left["employeeId"]},
        )
        conn.execute(
            text("UPDATE hr.employee SET mobile = NULL WHERE employee_id = CAST(:e AS uuid)"),
            {"e": no_mobile["employeeId"]},
        )
    from hrmgmt.provisioning import FoundLogin

    world.app.state.provisioner.existing[known["personalEmail"]] = FoundLogin(
        str(uuid.uuid4()), "Already There", "ACTIVE"
    )
    creator.calls.clear()
    got = create(world, hr, left, no_mobile, a, b, known)
    assert got[left["employeeId"]]["reason"] == "EMPLOYEE_NOT_ACTIVE"
    assert got[no_mobile["employeeId"]]["reason"] == "NO_MOBILE"
    assert got[a["employeeId"]]["reason"] == "MOBILE_SHARED"
    assert got[b["employeeId"]]["reason"] == "MOBILE_SHARED"
    assert got[known["employeeId"]]["reason"] == "LOGIN_EXISTS"
    assert all(g["outcome"] == "SKIPPED" for g in got.values())
    assert creator.calls == []


@pytest.mark.parametrize("code", ["EMAIL_OR_MOBILE_EXISTS", "SECURITY_UNAVAILABLE"])
def test_a_refused_attempt_is_tried_once_and_the_reason_is_kept(world, code):
    hr = hr_user(world)
    creator = Creator(world, fail_with=code)
    first = no_login_employee(world, hr)
    second = no_login_employee(world, hr)
    creator.calls.clear()
    got = create(world, hr, first, second)
    assert got[first["employeeId"]] == {
        **got[first["employeeId"]],
        "outcome": "FAILED",
        "reason": code,
    }
    assert got[second["employeeId"]]["outcome"] == "FAILED"
    assert len(creator.calls) == 2  # one attempt each, none repeated
    assert row(world, first) == (None, "FAILED")
    blocked = unmatched(sync(world, hr), first)
    if code == "EMAIL_OR_MOBILE_EXISTS":
        assert blocked["canCreate"] is False and blocked["blocked"] == code
    else:
        assert blocked["canCreate"] is True  # a temporary problem may be tried again by HR


def test_at_most_five_employees_per_request_and_only_hr_who_manage_may_create(world):
    hr = hr_user(world)
    too_many = [str(uuid.uuid4()) for _ in range(6)]
    r = world.client.post(
        "/hr/v1/employees/sync-users/create",
        json={"employeeIds": too_many},
        headers=world.headers(hr),
    )
    assert r.status_code == 422
    reader = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_READ)
    r = world.client.post(
        "/hr/v1/employees/sync-users/create",
        json={"employeeIds": [too_many[0]]},
        headers=world.headers(reader),
    )
    assert r.status_code == 403
    unknown = create_unknown(world, hr)
    assert unknown["outcome"] == "SKIPPED" and unknown["reason"] == "NOT_FOUND"


def create_unknown(world, hr):
    r = world.client.post(
        "/hr/v1/employees/sync-users/create",
        json={"employeeIds": ["not-an-id"]},
        headers=world.headers(hr),
    )
    assert r.status_code == 200
    return r.json()["results"][0]


def test_the_check_shows_a_login_whose_email_differs_from_the_hr_email(world):
    hr = hr_user(world)
    u = user(world, email="old.address@example.com", is_employee=True)
    emp = employee(world, hr, linked_to=u)
    body = sync(world, hr)
    shown = item(body, emp)
    assert shown["emailDiffers"] is True and shown["userEmail"] == "old.address@example.com"
    assert body["summary"]["emailDiffers"] >= 1


def test_the_counts_for_the_employees_page(world):
    hr = hr_user(world)
    before = world.client.get("/hr/v1/employees/summary", headers=world.headers(hr)).json()
    no_login_employee(world, hr)
    linked, _ = world.employee(hr, mobile=_mobile())
    gone = no_login_employee(world, hr)
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.employee SET employment_status = 'EXITED' WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": gone["employeeId"]},
        )
    after = world.client.get("/hr/v1/employees/summary", headers=world.headers(hr)).json()
    assert after["total"] == before["total"] + 3
    assert after["active"] == before["active"] + 2
    assert after["activeWithLogin"] == before["activeWithLogin"] + 1
    assert after["activeWithoutLogin"] == before["activeWithoutLogin"] + 1
    assert linked["employeeId"]
