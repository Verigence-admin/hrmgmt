"""Caution days (product owner, 21 Nov 2026): bad weather, a cyclone or a strike. The team is offered
voluntary leave and works the coming Sunday instead; whoever works the caution day keeps that
Sunday off, in lieu, with no leave request and no comp-off."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text

from hrmgmt import leave_rules as lr
from hrmgmt import permissions as perm
from tests.support import OUTLET, World, ist
from tests.test_attendance import send, token
from tests.test_holidays import at, manager, other_project_pc
from tests.test_leave import apply, balance, decide, seed

THU = date(2026, 11, 12)
SUN = date(2026, 11, 15)


@pytest.fixture()
def world(migrated_engine):
    w = World(migrated_engine, now=ist(2026, 11, 9, 10, 0))  # Monday 9 Nov 2026
    w.clean_assignments()
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE hr.leave_ledger, hr.leave_request, hr.leave_accrual,"
                " hr.attendance_exception, hr.attendance_day, hr.capture_token CASCADE"
            )
        )
        conn.execute(text("DELETE FROM hr.setting"))
        conn.execute(text("DELETE FROM hr.holiday WHERE created_by IS NOT NULL"))
    return w


def team(world: World):
    """Three PCs and a TL on project A (tenant-a)."""
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    people = {}
    for key in ("worker", "stayer", "on_leave"):
        emp, user = world.employee(hr)
        world.assign(user, "PC", outlet=OUTLET)
        people[key] = (emp["employeeId"], user)
    _, tl = world.employee(hr)
    world.assign(tl, "TL")
    return people, tl


def declare(world: World, hr: str | None = None, **body):
    body.setdefault("date", THU.isoformat())
    body.setdefault("reason", "Cyclone warning")
    body.setdefault("appliesTo", "SELECTED")
    body.setdefault("scope", [{"tenantId": "tenant-a"}])
    return world.client.post(
        "/hr/v1/holidays/caution", json=body, headers=world.headers(hr or manager(world))
    )


def calendar(world: World, user: str) -> dict[str, dict]:
    cal = world.client.get(
        "/hr/v1/attendance/calendar/me?month=2026-11", headers=world.headers(user)
    ).json()
    return {d["date"]: d for w in cal["weeks"] for d in w["days"]}


def test_a_caution_day_comes_with_its_working_sunday_and_its_shape_is_checked(world):
    team(world)
    hr = manager(world)
    assert declare(world, hr, date=SUN.isoformat()).status_code == 422  # not on a Sunday
    bad = declare(world, hr, workingSunday="2026-11-14")
    assert bad.status_code == 422 and "Sunday after" in bad.json()["detail"]
    made = declare(world, hr)
    assert made.status_code == 201, made.text
    caution, sunday = made.json()["caution"], made.json()["workingSunday"]
    assert caution["kind"] == "CAUTION" and caution["date"] == THU.isoformat()
    assert sunday["kind"] == "WORKING_DAY" and sunday["date"] == SUN.isoformat()
    assert sunday["name"] == "Cyclone warning" and sunday["cautionId"] == caution["holidayId"]
    # the pair is changed together, and removing one removes both
    change = world.client.put(
        f"/hr/v1/holidays/entries/{sunday['holidayId']}",
        json={
            "date": SUN.isoformat(),
            "name": "Other",
            "status": "DECLARED",
            "kind": "WORKING_DAY",
        },
        headers=world.headers(hr),
    )
    assert change.status_code == 422
    gone = world.client.delete(
        f"/hr/v1/holidays/entries/{sunday['holidayId']}", headers=world.headers(hr)
    )
    assert gone.status_code == 200
    items = world.client.get(
        "/hr/v1/holidays?year=2026&includeWorkingDays=true", headers=world.headers(hr)
    ).json()["items"]
    assert "Cyclone warning" not in {i["name"] for i in items}
    # earlier app versions never see a caution day in the list
    declare(world, hr)
    old = world.client.get("/hr/v1/holidays?year=2026", headers=world.headers(hr)).json()
    assert "Cyclone warning" not in {i["name"] for i in old["items"]}


def test_who_works_the_caution_day_keeps_the_sunday_off_and_the_rest_work_it(world):
    people, _ = team(world)
    (worker, worker_user), (stayer, stayer_user) = people["worker"], people["stayer"]
    other, other_user = other_project_pc(world)
    at(world, 8, 0, day=THU)
    assert declare(world).status_code == 201
    at(world, 11, 30, day=THU)  # late, but coming in is voluntary: nothing to approve
    r = send(world, worker_user, "in", token(world, worker_user))
    assert r.status_code == 200, r.text
    assert r.json()["needsApproval"] == []
    at(world, 18, 0, day=date(2026, 11, 16))
    mine, theirs = calendar(world, worker_user), calendar(world, stayer_user)
    elsewhere = calendar(world, other_user)
    thu, sun = THU.isoformat(), SUN.isoformat()
    # the worker: an ordinary working day, then the Sunday off in lieu
    assert mine[thu]["dayKind"] == "WORKING" and mine[thu]["caution"]["status"] == "WORKED"
    assert mine[sun]["dayKind"] == "SUNDAY" and mine[sun]["status"] != "ABSENT"
    assert mine[sun]["caution"] == {
        "status": "IN_LIEU",
        "reason": "Cyclone warning",
        "cautionDate": thu,
    }
    # who stayed home: the caution day off, the Sunday a working day (missed: absent)
    assert theirs[thu]["dayKind"] == "HOLIDAY" and theirs[thu]["status"] != "ABSENT"
    assert theirs[thu]["caution"]["status"] == "OFF"
    assert theirs[sun]["dayKind"] == "WORKING" and theirs[sun]["status"] == "ABSENT"
    assert theirs[sun]["workingSunday"] == {"name": "Cyclone warning", "inPlaceOf": thu}
    assert theirs[sun]["caution"]["status"] == "SUNDAY_WORK"
    # another project is untouched
    assert elsewhere[thu]["caution"] is None and elsewhere[sun]["dayKind"] == "SUNDAY"
    with world.engine.connect() as conn:
        # Thursday to Sunday: three working days for everyone, whichever two of the four
        assert lr.leave_days(conn, THU, SUN, False, worker) == 3
        assert lr.leave_days(conn, THU, SUN, False, stayer) == 3
        assert lr.leave_days(conn, THU, SUN, False, other) == 3
        # no comp-off is waiting for the caution day worked
        assert not conn.execute(
            text(
                "SELECT 1 FROM hr.attendance_exception WHERE employee_id = CAST(:e AS uuid)"
                " AND kind = 'OFF_DAY_WORK'"
            ),
            {"e": worker},
        ).first()


def test_leave_raised_for_the_caution_day_is_given_back(world):
    people, tl = team(world)
    (_, stayer_user), (on_leave, on_leave_user) = people["stayer"], people["on_leave"]
    seed(world, on_leave, 2)
    at(world, 9, 0, day=date(2026, 11, 11))
    unpaid = apply(world, stayer_user, leave_type="UNPAID", from_date=THU.isoformat()).json()
    paid = apply(
        world, on_leave_user, from_date=THU.isoformat(), to_date="2026-11-13"
    ).json()  # Thursday and Friday
    assert decide(world, tl, paid["requestId"]).status_code == 200
    assert balance(world, on_leave_user)["balance"] == 0.0
    at(world, 8, 0, day=THU)
    assert declare(world).status_code == 201
    with world.engine.connect() as conn:
        rows = {
            str(r[0]): (r[1], float(r[2]))
            for r in conn.execute(text("SELECT request_id, status, days FROM hr.leave_request"))
        }
    assert rows[unpaid["requestId"]][0] == "CANCELLED"  # nothing left to take
    assert rows[paid["requestId"]] == ("APPROVED", 1.0)  # only Friday now
    assert balance(world, on_leave_user)["balance"] == 1.0  # Thursday given back
