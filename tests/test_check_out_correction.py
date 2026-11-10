"""A missed check-out, recorded once the Team Lead approves it (product owner, 10 Nov 2026)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from hrmgmt.payroll_data import present_days
from tests.support import ist
from tests.test_attendance import send, token
from tests.test_attendance_correction import decide, team
from tests.test_attendance_correction import world as _world_fixture

world = pytest.fixture(name="world")(_world_fixture.__wrapped__)

DAY = "2026-10-07"


def ask_out(world, user, day=DAY, at="18:40", reason="Phone died at the outlet"):
    return world.client.post(
        "/hr/v1/attendance/corrections",
        json={"workDate": day, "requested": "CHECK_OUT", "checkOutTime": at, "reason": reason},
        headers=world.headers(user),
    )


def checked_in(world, pc, day=(2026, 10, 7)):
    world.clock.set(ist(*day, 9, 55))
    assert send(world, pc, "in", token(world, pc)).status_code == 200


def _day(world, emp, day=DAY):
    with world.engine.connect() as conn:
        return (
            conn.execute(
                text(
                    "SELECT check_out_at, check_out_flags FROM hr.attendance_day"
                    " WHERE employee_id = CAST(:e AS uuid) AND work_date = :d"
                ),
                {"e": emp["employeeId"], "d": day},
            )
            .mappings()
            .one()
        )


def test_a_missed_check_out_goes_to_the_team_lead_and_is_recorded_once_approved(world):
    emp, pc, tl, _, _ = team(world)
    checked_in(world, pc)
    world.clock.set(ist(2026, 10, 9, 12, 0))
    asked = ask_out(world, pc)
    assert asked.status_code == 200, asked.text
    assert asked.json()["requested"] == "CHECK_OUT"
    assert datetime.fromisoformat(asked.json()["checkOutAt"]) == ist(2026, 10, 7, 18, 40)
    (item,) = world.client.get(
        "/hr/v1/approvals/attendance-corrections", headers=world.headers(tl)
    ).json()["items"]
    assert datetime.fromisoformat(item["checkOutAt"]) == ist(2026, 10, 7, 18, 40)
    assert item["day"]["checkOutAt"] is None
    assert _day(world, emp)["check_out_at"] is None  # nothing changes until it is approved

    assert decide(world, tl, item["correctionId"]).json()["status"] == "APPROVED"
    day = _day(world, emp)
    assert day["check_out_at"] == ist(2026, 10, 7, 18, 40)
    assert day["check_out_flags"] == ["APPROVED_CORRECTION"]
    with world.engine.connect() as conn:
        assert present_days(conn, emp["employeeId"], date(2026, 10, 1)) == Decimal(1)


def test_today_can_be_asked_for_before_the_day_closes(world):
    emp, pc, tl, _, _ = team(world)
    checked_in(world, pc, (2026, 10, 9))
    world.clock.set(ist(2026, 10, 9, 18, 45))
    asked = ask_out(world, pc, day="2026-10-09", at="18:30")
    assert asked.status_code == 200, asked.text
    cid = asked.json()["correctionId"]
    assert decide(world, tl, cid).json()["status"] == "APPROVED"
    assert _day(world, emp, "2026-10-09")["check_out_at"] == ist(2026, 10, 9, 18, 30)


def test_a_rejected_check_out_leaves_the_day_as_it_was(world):
    emp, pc, tl, _, _ = team(world)
    checked_in(world, pc)
    world.clock.set(ist(2026, 10, 9, 12, 0))
    cid = ask_out(world, pc).json()["correctionId"]
    assert decide(world, tl, cid, "REJECT", "You left at 15:00").json()["status"] == "REJECTED"
    assert _day(world, emp)["check_out_at"] is None


def test_what_cannot_be_asked_for(world):
    emp, pc, tl, _, _ = team(world)
    world.clock.set(ist(2026, 10, 9, 12, 0))
    # no check-in that day
    no_in = ask_out(world, pc, day="2026-10-06")
    assert no_in.status_code == 422 and "no check-in" in no_in.json()["detail"]
    checked_in(world, pc)
    world.clock.set(ist(2026, 10, 9, 12, 0))
    assert "after your check-in" in ask_out(world, pc, at="09:30").json()["detail"]
    missing = world.client.post(
        "/hr/v1/attendance/corrections",
        json={"workDate": DAY, "requested": "CHECK_OUT", "reason": "Forgot"},
        headers=world.headers(pc),
    )
    assert "time you left" in missing.json()["detail"]
    # later than now, for today
    checked_in(world, pc, (2026, 10, 9))
    world.clock.set(ist(2026, 10, 9, 16, 0))
    assert "later than now" in ask_out(world, pc, day="2026-10-09", at="18:00").json()["detail"]
    # one waiting request per day
    assert ask_out(world, pc).status_code == 200
    assert ask_out(world, pc).status_code == 409


def test_approving_after_the_person_checked_out_records_nothing(world):
    emp, pc, tl, _, _ = team(world)
    checked_in(world, pc, (2026, 10, 9))
    world.clock.set(ist(2026, 10, 9, 13, 0))
    cid = ask_out(world, pc, day="2026-10-09", at="12:50").json()["correctionId"]
    world.clock.set(ist(2026, 10, 9, 18, 30))
    assert send(world, pc, "out", token(world, pc, "CHECK_OUT")).status_code == 200
    late = decide(world, tl, cid)
    assert late.status_code == 409
    assert _day(world, emp, "2026-10-09")["check_out_at"] == ist(2026, 10, 9, 18, 30)


def test_the_calendar_shows_it_waiting_then_present(world):
    emp, pc, tl, _, _ = team(world)
    checked_in(world, pc)
    world.clock.set(ist(2026, 10, 9, 12, 0))

    def status():
        cal = world.client.get(
            "/hr/v1/attendance/calendar/me", params={"month": "2026-10"}, headers=world.headers(pc)
        ).json()
        return next(d for w in cal["weeks"] for d in w["days"] if d["date"] == DAY)["status"]

    assert status() == "MISSING_CHECK_OUT"
    cid = ask_out(world, pc).json()["correctionId"]
    assert status() == "PENDING_APPROVAL"
    decide(world, tl, cid)
    assert status() == "PRESENT"
