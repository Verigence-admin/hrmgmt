"""Attendance summary for a month (product owner, 21 Nov 2026): one row per person with working
days, present, absent, leave applied and taken and hours; everyone sees their own, leads their
projects, HR and the CEO everyone."""

from __future__ import annotations

import uuid
from datetime import date

from hrmgmt import permissions as perm
from tests.support import World
from tests.test_attendance import send, token
from tests.test_caution_day import team, world  # noqa: F401
from tests.test_holidays import at, other_project_pc
from tests.test_leave import apply, decide, seed

MON, TUE, WED = date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)


def work(w: World, user: str, day: date, start=(9, 30), end=(19, 0)) -> None:
    at(w, *start, day=day)
    assert send(w, user, "in", token(w, user)).status_code == 200
    at(w, *end, day=day)
    assert send(w, user, "out", token(w, user, "CHECK_OUT")).status_code == 200


def test_the_month_in_numbers_for_self_leads_and_hr(world):  # noqa: F811
    people, tl = team(world)
    (worker, worker_user), (stayer, stayer_user) = people["worker"], people["stayer"]
    other, other_user = other_project_pc(world)
    seed(world, worker, 1)
    at(world, 9, 0, day=MON)
    paid = apply(world, worker_user, from_date=TUE.isoformat()).json()
    assert decide(world, tl, paid["requestId"]).status_code == 200
    assert (
        apply(world, stayer_user, leave_type="UNPAID", from_date=TUE.isoformat()).status_code == 201
    )
    work(world, worker_user, MON)  # 9 h 30 min
    work(world, worker_user, WED, end=(18, 30))  # 9 h
    at(world, 18, 0, day=WED)

    mine = world.client.get(
        "/hr/v1/attendance/summary/me?month=2026-11", headers=world.headers(worker_user)
    )
    assert mine.status_code == 200, mine.text
    row = mine.json()["employee"]
    assert row["workingDays"] == 25 and row["workingDaysSoFar"] == 9  # 30 days, 5 Sundays
    # 2 to 7 Nov were working days before any check-in here: the system marked them absent
    assert row["presentDays"] == 2 and row["absentDays"] == 6
    assert row["paidLeaveDays"] == 1 and row["leaveTakenDays"] == 1
    assert row["leaveAppliedDays"] == 0 and row["minutes"] == 570 + 540

    # a PC has no team view
    assert (
        world.client.get(
            "/hr/v1/attendance/summary/team", headers=world.headers(stayer_user)
        ).status_code
        == 403
    )
    lead = world.client.get(
        "/hr/v1/attendance/summary/team?month=2026-11", headers=world.headers(tl)
    ).json()
    rows = {r["employeeId"]: r for r in lead["people"]}
    assert worker in rows and stayer in rows and other not in rows  # only the TL's project
    assert rows[stayer]["absentDays"] == 6 + 2  # and this Monday and Wednesday
    assert rows[stayer]["leaveAppliedDays"] == 1 and rows[stayer]["unpaidLeaveDays"] == 0
    assert lead["totals"]["absentDays"] >= 2

    hr = world.grant(str(uuid.uuid4()), perm.HR_ATTENDANCE_READ_ALL)
    everyone = world.client.get(
        "/hr/v1/attendance/summary/team?month=2026-11", headers=world.headers(hr)
    ).json()
    assert {worker, stayer, other} <= {r["employeeId"] for r in everyone["people"]}
    only_b = world.client.get(
        "/hr/v1/attendance/summary/team?month=2026-11&projectCode=B", headers=world.headers(hr)
    ).json()
    assert [r["employeeId"] for r in only_b["people"]] == [other]
    assert other_user  # the other project's PC exists only to be left out of the TL's view
