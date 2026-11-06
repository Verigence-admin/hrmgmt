"""Attendance correction requests and the team's daily view (product owner, 6 Nov 2026)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from hrmgmt import attendance_grace as grace
from hrmgmt import permissions as perm
from hrmgmt.leave_rules import attendance_units
from hrmgmt.payroll_data import present_days
from tests.support import World, ist
from tests.test_attendance import send, setup_pc, token

TODAY = ist(2026, 10, 9, 12, 0)  # a Friday


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


def team(world: World):
    emp, pc, hr = setup_pc(world)
    _, tl = world.employee(hr)
    world.assign(tl, "TL")
    _, pm = world.employee(hr)
    world.assign(pm, "PM")
    return emp, pc, tl, pm, hr


def ask(world, user, day: str, requested="FULL_DAY", reason="Forgot to check in"):
    return world.client.post(
        "/hr/v1/attendance/corrections",
        json={"workDate": day, "requested": requested, "reason": reason},
        headers=world.headers(user),
    )


def decide(world, user, cid, decision="APPROVE", note=None):
    return world.client.post(
        f"/hr/v1/approvals/attendance-corrections/{cid}/decision",
        json={"decision": decision, "note": note},
        headers=world.headers(user),
    )


def test_an_absent_day_can_be_corrected_to_a_full_day_and_counts_in_payroll(world):
    emp, pc, tl, _, _ = team(world)
    world.clock.set(TODAY)
    r = ask(world, pc, "2026-10-07")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "PENDING" and r.json()["requested"] == "FULL_DAY"
    items = world.client.get(
        "/hr/v1/approvals/attendance-corrections", headers=world.headers(tl)
    ).json()["items"]
    assert len(items) == 1 and items[0]["day"]["checkInAt"] is None
    # the person cannot decide their own, and someone off the project cannot see it
    assert decide(world, pc, items[0]["correctionId"]).status_code == 404
    assert decide(world, tl, items[0]["correctionId"]).json()["status"] == "APPROVED"
    with world.engine.connect() as conn:
        assert present_days(conn, emp["employeeId"], date(2026, 10, 1)) == Decimal(1)
        units = attendance_units(conn, emp["employeeId"], None, date(2026, 10, 9))
    assert units == [(date(2026, 10, 7), Decimal(1))]
    mine = world.client.get("/hr/v1/attendance/corrections/me", headers=world.headers(pc)).json()
    assert [i["status"] for i in mine["items"]] == ["APPROVED"]


def test_a_rejected_half_day_corrected_to_full_day_loses_nothing(world):
    emp, pc, tl, _, _ = team(world)
    world.clock.set(ist(2026, 10, 7, 11, 40))
    send(world, pc, "in", token(world, pc), reason="Puncture")
    world.clock.set(ist(2026, 10, 7, 18, 30))
    send(world, pc, "out", token(world, pc, "CHECK_OUT"))
    (late,) = world.client.get("/hr/v1/approvals/attendance", headers=world.headers(tl)).json()[
        "items"
    ]
    world.client.post(
        f"/hr/v1/approvals/attendance/{late['exceptionId']}/decision",
        json={"decision": "REJECT", "note": "No call made"},
        headers=world.headers(tl),
    )
    with world.engine.connect() as conn:
        assert grace.half_days(conn, emp["employeeId"], date(2026, 10, 1)) == Decimal("0.5")
    world.clock.set(TODAY)
    cid = ask(world, pc, "2026-10-07", reason="I had called the TL").json()["correctionId"]
    decide(world, tl, cid)
    with world.engine.connect() as conn:
        assert grace.half_days(conn, emp["employeeId"], date(2026, 10, 1)) == 0
        assert present_days(conn, emp["employeeId"], date(2026, 10, 1)) == Decimal(1)
        units = attendance_units(conn, emp["employeeId"], None, date(2026, 10, 9))
    assert units == [(date(2026, 10, 7), Decimal(1))]


def test_a_half_day_correction_counts_half(world):
    emp, pc, tl, _, _ = team(world)
    world.clock.set(TODAY)
    cid = ask(world, pc, "2026-10-08", "HALF_DAY", "Came after the doctor").json()["correctionId"]
    decide(world, tl, cid)
    with world.engine.connect() as conn:
        assert present_days(conn, emp["employeeId"], date(2026, 10, 1)) == Decimal("0.5")


def test_what_cannot_be_corrected(world):
    emp, pc, tl, _, hr = team(world)
    world.clock.set(TODAY)
    code = lambda r: (r.status_code, r.json()["code"])  # noqa: E731
    assert code(ask(world, pc, "2026-10-09")) == (422, "ATTENDANCE_CORRECTION_NOT_ALLOWED")  # today
    assert code(ask(world, pc, "2026-10-04"))[1] == "ATTENDANCE_CORRECTION_NOT_ALLOWED"  # Sunday
    assert code(ask(world, pc, "2026-08-29"))[1] == "ATTENDANCE_CORRECTION_NOT_ALLOWED"  # too old
    # a complete, clean day already counts in full
    world.clock.set(ist(2026, 10, 6, 10, 20))
    send(world, pc, "in", token(world, pc))
    world.clock.set(ist(2026, 10, 6, 18, 30))
    send(world, pc, "out", token(world, pc, "CHECK_OUT"))
    world.clock.set(TODAY)
    full = ask(world, pc, "2026-10-06")
    assert full.status_code == 422 and "already counts as a full day" in full.json()["detail"]
    # one live request per day; a withdrawn one frees the day
    first = ask(world, pc, "2026-10-05").json()
    assert ask(world, pc, "2026-10-05").json()["code"] == "ATTENDANCE_CORRECTION_EXISTS"
    cancel = world.client.post(
        f"/hr/v1/attendance/corrections/{first['correctionId']}/cancel",
        headers=world.headers(pc),
    )
    assert cancel.json()["status"] == "CANCELLED"
    assert ask(world, pc, "2026-10-05").status_code == 200
    # a rejection needs a note
    cid = ask(world, pc, "2026-10-08").json()["correctionId"]
    assert decide(world, tl, cid, "REJECT").json()["code"] == "APPROVAL_NOTE_REQUIRED"
    assert decide(world, tl, cid, "REJECT", "Not at work").json()["status"] == "REJECTED"
    # once payroll for the month is submitted, the month is closed
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hr.payroll_run (pay_month, status, statutory_config, created_by)"
                " VALUES ('2026-10-01', 'SUBMITTED', '{}'::jsonb, 'test')"
            )
        )
    try:
        locked = ask(world, pc, "2026-10-02")
        assert locked.status_code == 422 and "already been submitted" in locked.json()["detail"]
    finally:
        with world.engine.begin() as conn:
            conn.execute(text("DELETE FROM hr.payroll_run WHERE created_by = 'test'"))


def test_a_pending_correction_waits_with_the_team_lead_for_the_deadline(world):
    from hrmgmt.api.oversight import _build

    _, pc, tl, pm, _ = team(world)
    world.clock.set(TODAY)
    ask(world, pc, "2026-10-07")
    with world.engine.connect() as conn:
        rows, _, by_approver = _build(conn, None, TODAY)
    assert rows[tl]["pending"]["ATTENDANCE"] == 1
    assert by_approver[tl][0]["detail"] == "CORRECTION_FULL_DAY"


def test_team_leads_and_executives_see_their_projects_day_summed_up(world):
    emp, pc, tl, pm, hr = team(world)
    _, other_pc = world.employee(hr)
    world.assign(other_pc, "PC", tenant="tenant-b", project=("P2", "Project Two"))
    _, executive = world.employee(hr)
    world.assign(executive, "EXECUTIVE")
    world.clock.set(ist(2026, 10, 8, 11, 30))
    send(world, pc, "in", token(world, pc), reason="Traffic")
    world.clock.set(TODAY)
    ask(world, pc, "2026-10-07")
    for viewer in (tl, pm, executive):
        r = world.client.get(
            "/hr/v1/attendance/team-daily",
            params={"date": "2026-10-08"},
            headers=world.headers(viewer),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        codes = {row["employeeCode"] for row in body["rows"]}
        assert emp["employeeCode"] in codes  # their PC
        assert all(row["projectCode"] == "P1" for row in body["rows"])  # not Project Two
        assert [p["projectCode"] for p in body["projects"]] == ["P1"]
        assert body["summary"]["late"] == 1 and body["summary"]["pendingApproval"] >= 1
        assert "checkInFaceScore" not in body["rows"][0]
    yesterday = world.client.get(
        "/hr/v1/attendance/team-daily", params={"date": "2026-10-07"}, headers=world.headers(tl)
    ).json()
    (row,) = [x for x in yesterday["rows"] if x["employeeCode"] == emp["employeeCode"]]
    assert row["status"] == "PENDING_APPROVAL" and row["correction"]["requested"] == "FULL_DAY"
    assert any(d["label"] == "Correction asked: full day" for d in row["delinquencies"])
    # a PC is not a lead; someone leading another project cannot ask for this one
    assert (
        world.client.get("/hr/v1/attendance/team-daily", headers=world.headers(pc)).status_code
        == 403
    )
    assert (
        world.client.get(
            "/hr/v1/attendance/team-daily",
            params={"projectCode": "P2"},
            headers=world.headers(tl),
        ).status_code
        == 403
    )
    # HR sees every project
    hr_view = world.client.get(
        "/hr/v1/attendance/team-daily",
        params={"date": "2026-10-08"},
        headers=world.headers(world.grant(str(uuid.uuid4()), perm.HR_ATTENDANCE_READ_ALL)),
    ).json()
    assert {p["projectCode"] for p in hr_view["projects"]} >= {"P1", "P2"}


def test_an_approved_correction_shows_as_corrected_in_hr_daily_view(world):
    emp, pc, tl, _, _ = team(world)
    world.clock.set(TODAY)
    decide(world, tl, ask(world, pc, "2026-10-07").json()["correctionId"])
    keeper = world.grant(str(uuid.uuid4()), perm.HR_ATTENDANCE_READ_ALL)
    daily = world.client.get(
        "/hr/v1/attendance/daily", params={"date": "2026-10-07"}, headers=world.headers(keeper)
    ).json()
    (row,) = [x for x in daily["rows"] if x["employeeCode"] == emp["employeeCode"]]
    assert row["status"] == "CORRECTED"
    assert [d["code"] for d in row["delinquencies"]] == ["CORRECTION_FULL_DAY"]
    assert daily["summary"]["corrected"] >= 1
