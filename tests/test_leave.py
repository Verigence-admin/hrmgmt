from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from hrmgmt import permissions as perm
from tests.support import OUTLET, World, ist


@pytest.fixture()
def world(migrated_engine):
    w = World(migrated_engine, now=ist(2026, 10, 5, 10, 0))  # Monday 5 Oct 2026
    w.clean_assignments()
    with migrated_engine.begin() as conn:
        conn.execute(text("TRUNCATE hr.leave_ledger, hr.leave_request"))
        conn.execute(text("DELETE FROM hr.setting"))
        conn.execute(text("DELETE FROM hr.holiday WHERE status = 'DECLARED'"))
    return w


def person(world: World, role: str | None = "PC", tenant="tenant-a", perms=()):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    emp, user = world.employee(hr)
    if role:
        world.assign(user, role, tenant=tenant, outlet=OUTLET if role == "PC" else None)
    if perms:
        world.grant(user, *perms)
    return emp, user


def apply(world, user, **body):
    body.setdefault("leave_type", "SICK")
    body.setdefault("from_date", "2026-10-06")
    body.setdefault("to_date", body["from_date"])
    return world.client.post("/hr/v1/leave/requests", json=body, headers=world.headers(user))


def decide(world, user, rid, decision="APPROVE", note=None):
    return world.client.post(
        f"/hr/v1/approvals/leave/{rid}/decision",
        json={"decision": decision, "note": note},
        headers=world.headers(user),
    )


def balance(world, user, leave_type="SICK"):
    b = world.client.get("/hr/v1/leave/balance?year=2026", headers=world.headers(user)).json()
    return next(t for t in b["types"] if t["leaveType"] == leave_type)


def test_everyone_starts_with_the_yearly_allowance(world):
    _, user = person(world)
    b = world.client.get("/hr/v1/leave/balance", headers=world.headers(user)).json()
    assert b["year"] == 2026
    assert {t["leaveType"]: t["balance"] for t in b["types"]} == {"SICK": 5.0, "EARNED": 5.0}
    # reading again does not grant again
    assert balance(world, user)["balance"] == 5.0


def test_a_range_counts_working_days_only(world):
    _, user = person(world)
    # Mon 12 Oct to Sun 18 Oct: six working days, the Sunday is not leave
    r = apply(world, user, leave_type="UNPAID", from_date="2026-10-12", to_date="2026-10-18")
    assert r.status_code == 201 and r.json()["days"] == 6.0


def test_sundays_and_holidays_are_not_leave_days(world):
    _, user = person(world)
    hr = world.grant(str(uuid.uuid4()), perm.HR_SETTINGS_MANAGE)
    # Tue 20 Oct is tentative, so it counts until HR declares it
    r = apply(world, user, leave_type="EARNED", from_date="2026-10-19", to_date="2026-10-20")
    assert r.json()["days"] == 2.0
    world.client.post(
        f"/hr/v1/leave/requests/{r.json()['requestId']}/cancel", headers=world.headers(user)
    )
    world.client.put(
        "/hr/v1/holidays/2026-10-20",
        json={"name": "Vijaya Dasami", "status": "DECLARED"},
        headers=world.headers(hr),
    )
    r2 = apply(world, user, leave_type="EARNED", from_date="2026-10-19", to_date="2026-10-20")
    assert r2.json()["days"] == 1.0
    only_sunday = apply(world, user, from_date="2026-10-25", to_date="2026-10-25")
    assert only_sunday.status_code == 422 and only_sunday.json()["code"] == "LEAVE_NO_WORKING_DAYS"


def test_half_day_and_date_rules(world):
    _, user = person(world)
    assert apply(world, user, from_date="2026-10-06", half_day=True).json()["days"] == 0.5
    assert (
        apply(world, user, from_date="2026-10-07", to_date="2026-10-08", half_day=True).status_code
        == 422
    )
    assert apply(world, user, from_date="2026-10-09", to_date="2026-10-08").status_code == 422
    assert apply(world, user, from_date="2026-12-30", to_date="2027-01-02").status_code == 422
    assert apply(world, user, from_date="2026-08-01").status_code == 422  # more than 30 days back
    assert apply(world, user, from_date="2028-01-10").status_code == 422  # too far ahead
    assert apply(world, user, from_date="2026-10-14", reason="x" * 600).status_code == 422


def test_overlapping_requests_are_refused_but_cancelled_ones_do_not_count(world):
    _, user = person(world)
    first = apply(world, user, from_date="2026-10-06", to_date="2026-10-07")
    assert first.status_code == 201
    assert apply(world, user, from_date="2026-10-07").json()["code"] == "LEAVE_OVERLAPS"
    world.client.post(
        f"/hr/v1/leave/requests/{first.json()['requestId']}/cancel", headers=world.headers(user)
    )
    assert apply(world, user, from_date="2026-10-07").status_code == 201


def test_pending_days_are_held_against_the_balance(world):
    _, user = person(world)
    assert apply(world, user, from_date="2026-10-06", to_date="2026-10-10").json()["days"] == 5.0
    again = apply(world, user, from_date="2026-10-12")
    assert again.status_code == 409 and again.json()["code"] == "LEAVE_BALANCE_TOO_LOW"
    b = balance(world, user)
    assert b["pending"] == 5.0 and b["available"] == 0.0 and b["balance"] == 5.0
    assert (
        apply(world, user, leave_type="UNPAID", from_date="2026-10-12").status_code == 201
    )  # unpaid has no balance


def test_a_pc_leave_goes_to_the_projects_team_lead_and_approval_deducts_once(world):
    _, pc = person(world, "PC")
    _, tl = person(world, "TL")
    _, other_tl = person(world, "TL", tenant="tenant-b")
    rid = apply(world, pc, from_date="2026-10-06", to_date="2026-10-07").json()["requestId"]
    assert [
        i["requestId"]
        for i in world.client.get("/hr/v1/approvals/leave", headers=world.headers(tl)).json()[
            "items"
        ]
    ] == [rid]
    assert (
        world.client.get("/hr/v1/approvals/leave", headers=world.headers(other_tl)).json()["items"]
        == []
    )
    assert decide(world, other_tl, rid).status_code == 404
    assert decide(world, pc, rid).status_code == 404  # not one's own
    assert decide(world, tl, rid, "REJECT").json()["code"] == "APPROVAL_NOTE_REQUIRED"
    assert decide(world, tl, rid).json()["status"] == "APPROVED"
    assert decide(world, tl, rid).status_code == 409
    b = balance(world, pc)
    assert b["balance"] == 3.0 and b["used"] == 2.0 and b["pending"] == 0.0


def test_rejection_keeps_the_balance_and_the_reason(world):
    _, pc = person(world, "PC")
    _, tl = person(world, "TL")
    rid = apply(world, pc, from_date="2026-10-06").json()["requestId"]
    assert decide(world, tl, rid, "REJECT", "Audit week").json()["status"] == "REJECTED"
    assert balance(world, pc)["balance"] == 5.0
    mine = world.client.get("/hr/v1/leave/requests", headers=world.headers(pc)).json()["items"]
    assert mine[0]["status"] == "REJECTED" and mine[0]["decisionNote"] == "Audit week"


def test_a_team_leads_leave_goes_to_a_project_manager_and_a_managers_to_the_ceo(world):
    _, tl = person(world, "TL")
    _, pm = person(world, "PM")
    rid = apply(world, tl, from_date="2026-10-06").json()["requestId"]
    assert (
        world.client.get("/hr/v1/approvals/leave", headers=world.headers(pm)).json()["items"][0][
            "requestId"
        ]
        == rid
    )
    assert decide(world, pm, rid).status_code == 200
    rid2 = apply(world, pm, from_date="2026-10-13").json()["requestId"]
    other_pm = person(world, "PM")[1]
    assert decide(world, other_pm, rid2).status_code == 404  # a manager's leave is for the CEO
    ceo = person(world, None, perms=[perm.HR_PAYROLL_APPROVE])[1]
    assert decide(world, ceo, rid2).status_code == 200


def test_a_team_lead_with_no_manager_falls_back_to_hr(world):
    _, tl = person(world, "TL")
    hr_reviewer = world.grant(str(uuid.uuid4()), perm.HR_LEAVE_REVIEW)
    rid = apply(world, tl, from_date="2026-10-06").json()["requestId"]
    assert decide(world, hr_reviewer, rid).status_code == 200


def test_unpaid_leave_and_people_without_a_project_go_to_hr_and_the_ceo_records_without_approval(
    world,
):
    _, pc = person(world, "PC")
    _, tl = person(world, "TL")
    hr_reviewer = world.grant(str(uuid.uuid4()), perm.HR_LEAVE_REVIEW)
    unpaid = apply(world, pc, leave_type="UNPAID", from_date="2026-10-06").json()
    assert unpaid["approverRule"] == "HR"
    assert decide(world, tl, unpaid["requestId"]).status_code == 404  # TL does not decide unpaid
    assert decide(world, hr_reviewer, unpaid["requestId"]).status_code == 200
    assert balance(world, pc)["balance"] == 5.0  # unpaid touches no balance
    _, staff = person(world, None)
    assert apply(world, staff, from_date="2026-10-06").json()["approverRule"] == "HR"
    _, boss = person(world, None, perms=[perm.HR_PAYROLL_APPROVE])
    own = apply(world, boss, from_date="2026-10-06").json()
    assert own["status"] == "APPROVED" and own["approverRule"] == "AUTO"
    assert balance(world, boss)["balance"] == 4.0


def test_hr_staff_leave_goes_to_the_ceo(world):
    _, hr_person = person(world, None, perms=[perm.HR_LEAVE_REVIEW])
    r = apply(world, hr_person, from_date="2026-10-06").json()
    assert r["approverRule"] == "CEO"
    colleague = world.grant(str(uuid.uuid4()), perm.HR_LEAVE_REVIEW)
    assert decide(world, colleague, r["requestId"]).status_code == 404


def test_cancel_only_while_pending_and_only_your_own(world):
    _, pc = person(world, "PC")
    _, tl = person(world, "TL")
    _, other = person(world, "PC")
    rid = apply(world, pc, from_date="2026-10-06").json()["requestId"]
    assert (
        world.client.post(
            f"/hr/v1/leave/requests/{rid}/cancel", headers=world.headers(other)
        ).status_code
        == 404
    )
    decide(world, tl, rid)
    assert (
        world.client.post(f"/hr/v1/leave/requests/{rid}/cancel", headers=world.headers(pc)).json()[
            "code"
        ]
        == "LEAVE_NOT_PENDING"
    )


def test_hr_can_reverse_an_approved_leave_and_adjust_with_a_note(world):
    _, pc = person(world, "PC")
    _, tl = person(world, "TL")
    reviewer = world.grant(str(uuid.uuid4()), perm.HR_LEAVE_REVIEW)
    rid = apply(world, pc, from_date="2026-10-06", to_date="2026-10-08").json()["requestId"]
    decide(world, tl, rid)
    assert balance(world, pc)["balance"] == 2.0
    assert (
        world.client.post(
            f"/hr/v1/leave/requests/{rid}/reverse", headers=world.headers(tl)
        ).status_code
        == 403
    )
    assert (
        world.client.post(
            f"/hr/v1/leave/requests/{rid}/reverse", headers=world.headers(reviewer)
        ).json()["status"]
        == "CANCELLED"
    )
    assert balance(world, pc)["balance"] == 5.0
    assert (
        world.client.post(
            f"/hr/v1/leave/requests/{rid}/reverse", headers=world.headers(reviewer)
        ).status_code
        == 409
    )
    emp_id = world.client.get("/hr/v1/leave/overview", headers=world.headers(reviewer)).json()[
        "items"
    ][0]["employeeId"]
    assert (
        world.client.post(
            f"/hr/v1/leave/employee/{emp_id}/adjust",
            json={"leave_type": "SICK", "year": 2026, "days": 1},
            headers=world.headers(reviewer),
        ).status_code
        == 422
    )  # a note is needed


def test_adjustment_changes_the_balance_through_the_ledger_and_history_cannot_be_edited(
    world, migrated_engine
):
    emp, pc = person(world, "PC")
    reviewer = world.grant(str(uuid.uuid4()), perm.HR_LEAVE_REVIEW)
    r = world.client.post(
        f"/hr/v1/leave/employee/{emp['employeeId']}/adjust",
        json={"leave_type": "EARNED", "year": 2026, "days": 2, "note": "Carried over"},
        headers=world.headers(reviewer),
    )
    assert r.status_code == 200
    assert next(t for t in r.json()["types"] if t["leaveType"] == "EARNED")["balance"] == 7.0
    assert (
        world.client.post(
            f"/hr/v1/leave/employee/{emp['employeeId']}/adjust",
            json={"leave_type": "EARNED", "year": 2026, "days": 0, "note": "none"},
            headers=world.headers(reviewer),
        ).status_code
        == 422
    )
    for statement in ("UPDATE hr.leave_ledger SET days = 99", "DELETE FROM hr.leave_ledger"):
        with pytest.raises(DBAPIError, match="append-only"):
            with migrated_engine.begin() as conn:
                conn.execute(text(statement))
    detail = world.client.get(
        f"/hr/v1/leave/employee/{emp['employeeId']}", headers=world.headers(reviewer)
    ).json()
    assert detail["balance"]["types"][1]["balance"] == 7.0
    assert (
        world.client.get(
            f"/hr/v1/leave/employee/{emp['employeeId']}", headers=world.headers(pc)
        ).status_code
        == 403
    )


def test_settings_change_the_allowance_for_new_grants(world):
    hr = world.grant(str(uuid.uuid4()), perm.HR_SETTINGS_MANAGE)
    world.client.put(
        "/hr/v1/settings",
        json={"values": {"leave.sick_days_per_year": 8}},
        headers=world.headers(hr),
    )
    _, user = person(world)
    assert balance(world, user)["balance"] == 8.0
