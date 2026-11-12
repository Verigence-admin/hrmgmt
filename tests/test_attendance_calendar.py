"""Hours clocked, the attendance calendar, the 17:00 system absence and the project-wise weekly view
(product owner, 6 Nov 2026)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from hrmgmt import permissions as perm
from tests.support import World, ist
from tests.test_attendance import OUTLET, send, setup_pc, token


@pytest.fixture()
def world(migrated_engine):
    w = World(migrated_engine)
    w.clean_assignments()
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM hr.attendance_correction"))
        conn.execute(text("DELETE FROM hr.attendance_exception"))
        conn.execute(text("DELETE FROM hr.attendance_day"))
        conn.execute(text("DELETE FROM hr.capture_token"))
        conn.execute(text("DELETE FROM hr.holiday WHERE status = 'DECLARED'"))
        conn.execute(text("DELETE FROM hr.setting"))
    return w


def leave(world, emp, day, status, half=False):
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hr.leave_request (employee_id, leave_type, from_date, to_date, days,"
                " half_day, status, approver_rule) VALUES (CAST(:e AS uuid), 'SICK', :d, :d,"
                " :n, :h, :s, 'HR')"
            ),
            {"e": emp["employeeId"], "d": day, "n": 0.5 if half else 1, "h": half, "s": status},
        )


def worked(world, user, came, left):
    world.clock.set(came)
    assert send(world, user, "in", token(world, user), reason="ok").status_code == 200
    world.clock.set(left)
    r = send(world, user, "out", token(world, user, "CHECK_OUT"), reason="ok")
    assert r.status_code == 200, r.text


def cell(body, day):
    for week in body["weeks"]:
        for c in week["days"]:
            if c["date"] == day:
                return c
    raise AssertionError(day)


def test_my_calendar_shows_hours_per_day_and_week_absence_and_leave(world):
    emp, pc, _ = setup_pc(world)
    worked(world, pc, ist(2026, 10, 5, 10, 15), ist(2026, 10, 5, 18, 45))  # 8 h 30 m
    worked(world, pc, ist(2026, 10, 6, 10, 20), ist(2026, 10, 6, 18, 20))  # 8 h
    leave(world, emp, "2026-10-07", "APPROVED")
    leave(world, emp, "2026-10-08", "PENDING")
    world.clock.set(ist(2026, 10, 9, 17, 5))  # Friday, after 17:00, no check-in
    body = world.client.get(
        "/hr/v1/attendance/calendar/me", params={"month": "2026-10"}, headers=world.headers(pc)
    ).json()
    assert body["month"] == "2026-10" and body["absentAfter"] == "17:00"
    # whole weeks, Monday first: 28 Sep to 1 Nov
    assert (
        body["weeks"][0]["weekStart"] == "2026-09-28"
        and body["weeks"][-1]["weekEnd"] == "2026-11-01"
    )
    assert cell(body, "2026-09-30")["inMonth"] is False
    assert (
        cell(body, "2026-10-05")["status"] == "PRESENT"
        and cell(body, "2026-10-05")["minutes"] == 510
    )
    assert cell(body, "2026-10-07")["status"] == "LEAVE_APPROVED"
    applied = cell(body, "2026-10-08")
    assert (
        applied["status"] == "LEAVE_APPLIED" and applied["label"] == "Leave applied (not approved)"
    )
    today = cell(body, "2026-10-09")
    assert today["status"] == "ABSENT" and today["markedBySystem"] is True
    assert cell(body, "2026-10-04")["status"] == "OFF_DAY"  # Sunday
    assert cell(body, "2026-10-12")["status"] == "UPCOMING"
    (week,) = [w for w in body["weeks"] if w["weekStart"] == "2026-10-05"]
    assert week["minutes"] == 990 and week["daysWorked"] == 2
    assert body["totals"]["minutes"] == 990


def test_before_17_today_is_not_yet_absent_and_running_hours_show(world):
    _, pc, _ = setup_pc(world)
    world.clock.set(ist(2026, 10, 9, 16, 55))
    body = world.client.get("/hr/v1/attendance/calendar/me", headers=world.headers(pc)).json()
    assert cell(body, "2026-10-09")["status"] == "NOT_CHECKED_IN"
    world.clock.set(ist(2026, 10, 9, 10, 30))
    send(world, pc, "in", token(world, pc))
    world.clock.set(ist(2026, 10, 9, 13, 0))
    body = world.client.get("/hr/v1/attendance/calendar/me", headers=world.headers(pc)).json()
    today = cell(body, "2026-10-09")
    assert today["status"] == "CHECKED_IN" and today["runningMinutes"] == 150
    assert today["minutes"] == 0 and body["totals"]["minutes"] == 0


def test_no_check_in_after_17_and_today_can_then_be_corrected(world):
    _, pc, _ = setup_pc(world)
    world.clock.set(ist(2026, 10, 9, 17, 0))
    r = send(world, pc, "in", token(world, pc), reason="late")
    assert r.status_code == 422 and r.json()["code"] == "ATTENDANCE_DAY_CLOSED"
    assert "marked Absent by the system" in r.json()["detail"]
    fix = world.client.post(
        "/hr/v1/attendance/corrections",
        json={"workDate": "2026-10-09", "requested": "FULL_DAY", "reason": "Phone died"},
        headers=world.headers(pc),
    )
    assert fix.status_code == 200, fix.text
    # a Sunday is a day off: work on it can still be marked after 17:00
    world.clock.set(ist(2026, 10, 11, 18, 0))
    assert send(world, pc, "in", token(world, pc)).status_code == 200


def test_hr_daily_view_marks_today_absent_after_17_and_shows_applied_leave(world):
    emp, pc, hr = setup_pc(world)
    other, _ = world.employee(hr)
    leave(world, other, "2026-10-09", "PENDING")
    keeper = world.grant(str(uuid.uuid4()), perm.HR_ATTENDANCE_READ_ALL)
    world.clock.set(ist(2026, 10, 9, 16, 0))
    rows = world.client.get(
        "/hr/v1/attendance/daily", params={"date": "2026-10-09"}, headers=world.headers(keeper)
    ).json()["rows"]
    mine = {r["employeeCode"]: r["status"] for r in rows}
    assert mine[emp["employeeCode"]] == "NOT_CHECKED_IN"
    assert mine[other["employeeCode"]] == "LEAVE_APPLIED"
    world.clock.set(ist(2026, 10, 9, 17, 1))
    rows = world.client.get(
        "/hr/v1/attendance/daily", params={"date": "2026-10-09"}, headers=world.headers(keeper)
    ).json()["rows"]
    assert {r["employeeCode"]: r["status"] for r in rows}[emp["employeeCode"]] == "ABSENT"


def lead_team(world):
    emp, pc, hr = setup_pc(world)
    _, tl = world.employee(hr)
    world.assign(tl, "TL")
    shared_emp, shared = world.employee(hr)
    world.assign(shared, "PC", outlet=OUTLET)
    world.assign(shared, "PC", outlet=("Puri Motors", 19.81, 85.83), project=("P2", "Project Two"))
    _, outsider = world.employee(hr)
    world.assign(outsider, "PC", project=("P3", "Project Three"))
    return emp, pc, tl, shared_emp, shared, hr


def test_weekly_view_is_project_wise_with_week_hours_and_shared_people(world):
    emp, pc, tl, shared_emp, shared, hr = lead_team(world)
    worked(world, pc, ist(2026, 10, 5, 10, 15), ist(2026, 10, 5, 18, 15))
    worked(world, pc, ist(2026, 10, 6, 10, 15), ist(2026, 10, 6, 16, 15))
    world.clock.set(ist(2026, 10, 7, 12, 0))
    body = world.client.get(
        "/hr/v1/attendance/team-weekly", params={"date": "2026-10-07"}, headers=world.headers(tl)
    ).json()
    assert body["weekStart"] == "2026-10-05" and len(body["days"]) == 7
    assert [p["projectCode"] for p in body["projects"]] == ["P1"]  # only what the TL leads
    people = {p["employeeCode"]: p for p in body["projects"][0]["people"]}
    me = people[emp["employeeCode"]]
    assert me["weekMinutes"] == 840 and me["daysWorked"] == 2 and me["shared"] is False
    assert [d["status"] for d in me["days"][:3]] == [
        "PRESENT",
        "PENDING_APPROVAL",  # 6 h and out before 17:00: early, waiting for the Team Lead
        "NOT_CHECKED_IN",
    ]
    other = people[shared_emp["employeeCode"]]
    assert other["shared"] is True and other["otherProjects"] == [
        {"projectCode": "P2", "projectName": "Project Two"}
    ]
    assert body["summary"]["shared"] == 1
    # HR sees every project, and the shared person under both
    keeper = world.grant(str(uuid.uuid4()), perm.HR_ATTENDANCE_READ_ALL)
    all_projects = world.client.get(
        "/hr/v1/attendance/team-weekly",
        params={"date": "2026-10-07"},
        headers=world.headers(keeper),
    ).json()["projects"]
    where = {
        p["projectCode"]
        for p in all_projects
        if any(x["employeeCode"] == shared_emp["employeeCode"] for x in p["people"])
    }
    assert where == {"P1", "P2"}
    assert (
        world.client.get("/hr/v1/attendance/team-weekly", headers=world.headers(pc)).status_code
        == 403
    )


def test_team_daily_rows_carry_hours_week_total_and_shared(world):
    emp, pc, tl, shared_emp, _, _ = lead_team(world)
    worked(world, pc, ist(2026, 10, 5, 10, 15), ist(2026, 10, 5, 18, 15))
    world.clock.set(ist(2026, 10, 6, 10, 20))
    send(world, pc, "in", token(world, pc))
    world.clock.set(ist(2026, 10, 6, 12, 20))
    body = world.client.get(
        "/hr/v1/attendance/team-daily", params={"date": "2026-10-06"}, headers=world.headers(tl)
    ).json()
    rows = body["rows"]
    mine = {r["employeeCode"]: r for r in rows}
    assert mine[emp["employeeCode"]]["weekMinutes"] == 480
    assert mine[emp["employeeCode"]]["runningMinutes"] == 120
    assert mine[shared_emp["employeeCode"]]["shared"] is True
    # The day's times, so the screen can say how late a check-in was (product owner, 9 Nov 2026).
    times = {
        "checkIn": "10:30",
        "lateAfter": "11:15",
        "checkOutEarliest": "17:00",
        "checkOut": "18:30",
        "absentAfter": "17:00",
        "minimumHours": 5.0,
    }
    assert body["standardTimes"] == times
    assert all(r["standardTimes"] == times for r in rows)
    # Face scores stay in HR's own view.
    assert all("checkInFaceScore" not in r and "checkOutFaceScore" not in r for r in rows)


def test_a_lead_sees_the_calendar_of_their_people_only(world):
    emp, pc, tl, _, _, hr = lead_team(world)
    world.clock.set(ist(2026, 10, 9, 12, 0))
    ok = world.client.get(
        f"/hr/v1/attendance/calendar/employee/{emp['employeeId']}", headers=world.headers(tl)
    )
    assert ok.status_code == 200 and ok.json()["employeeCode"] == emp["employeeCode"]
    outsider, _ = world.employee(hr)
    assert (
        world.client.get(
            f"/hr/v1/attendance/calendar/employee/{outsider['employeeId']}",
            headers=world.headers(tl),
        ).status_code
        == 404
    )
    assert (
        world.client.get(
            f"/hr/v1/attendance/calendar/employee/{emp['employeeId']}", headers=world.headers(pc)
        ).status_code
        == 403
    )


# ---- the person's record with an approval (product owner, 12 Nov 2026) ------------------------


def test_an_approval_carries_the_average_hours_a_week_over_the_six_weeks_before(world):
    """ "When approval goes, it should have record how many leaves a person has took in last 4 weeks
    and how much is the average working hours per week for last 6 weeks"."""
    from datetime import date

    from hrmgmt import settings_store as cfg
    from hrmgmt.attendance_calendar import weekly_hours

    emp, pc, tl, *_ = lead_team(world)
    eid = emp["employeeId"]
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.employee SET date_of_joining = '2026-09-01' WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": eid},
        )
    worked(world, pc, ist(2026, 10, 5, 10, 0), ist(2026, 10, 5, 18, 0))  # 8 h, week of 5 Oct
    worked(world, pc, ist(2026, 10, 6, 10, 0), ist(2026, 10, 6, 16, 0))  # 6 h, same week
    worked(world, pc, ist(2026, 11, 2, 10, 0), ist(2026, 11, 2, 19, 0))  # 9 h, week of 2 Nov
    worked(world, pc, ist(2026, 11, 9, 10, 0), ist(2026, 11, 9, 18, 0))  # this week: not counted
    world.clock.set(ist(2026, 11, 10, 12, 0))
    with world.engine.connect() as conn:
        hours = weekly_hours(
            conn, eid, date(2026, 11, 20), now=world.clock(), values=cfg.load_all(conn)
        )
    assert (hours["windowFrom"], hours["windowTo"], hours["weeks"]) == (
        "2026-09-28",
        "2026-11-08",
        6,
    )
    assert [w["hours"] for w in hours["byWeek"]] == [0.0, 14.0, 0.0, 0.0, 0.0, 9.0]
    assert hours["averageHours"] == 3.8  # 23 h over 6 weeks
    # A new joiner: only the weeks from joining count.
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.employee SET date_of_joining = '2026-10-29' WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": eid},
        )
    with world.engine.connect() as conn:
        joiner = weekly_hours(
            conn, eid, date(2026, 11, 20), now=world.clock(), values=cfg.load_all(conn)
        )
    assert joiner["weeks"] == 2 and joiner["averageHours"] == 4.5
    # The approver sees both figures with a correction request (and with a leave request: the
    # same two calls in leave_approvals).
    raised = world.client.post(
        "/hr/v1/attendance/corrections",
        json={"workDate": "2026-11-04", "requested": "FULL_DAY", "reason": "At the outlet all day"},
        headers=world.headers(pc),
    )
    assert raised.status_code == 200, raised.text
    items = world.client.get(
        "/hr/v1/approvals/attendance-corrections", headers=world.headers(tl)
    ).json()["items"]
    [mine] = [i for i in items if i["employeeId"] == eid]
    assert mine["weeklyHours"]["windowTo"] == "2026-11-01"  # the 6 whole weeks before 4 Nov's week
    assert mine["recentLeave"]["windowFrom"] == "2026-10-07"  # the 4 weeks before 4 Nov
