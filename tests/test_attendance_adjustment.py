"""Correcting a check-in or check-out time or a location, the month's late and early figures, and
the monthly limits that send requests to a Project Manager or Executive (product owner,
16 Nov 2026)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

import pytest
from sqlalchemy import text

from hrmgmt import attendance_grace as grace
from hrmgmt import permissions as perm
from hrmgmt.approver_deadline import overdue_team_leads
from hrmgmt.attendance_month import TEAM_LEAD_NOTE, month_stats
from hrmgmt.provisioning import UserSummary
from hrmgmt.timeutil import IST
from tests.support import FAR, World, ist, without_month_limits
from tests.test_attendance import send, setup_pc, token
from tests.test_attendance_correction import decide
from tests.test_short_day import FakeMailer


@pytest.fixture()
def world(migrated_engine):
    w = World(migrated_engine)
    w.clean_assignments()
    with migrated_engine.begin() as conn:
        for table in (
            "attendance_escalation",
            "attendance_correction",
            "attendance_exception",
            "attendance_day",
            "capture_token",
            "message_log",
            "setting",
        ):
            conn.execute(text(f"DELETE FROM hr.{table}"))
        conn.execute(text("DELETE FROM hr.holiday WHERE status = 'DECLARED'"))
    w.mailer = FakeMailer()
    w.app.state.mailer = w.mailer
    return w


def team(world: World):
    emp, pc, hr = setup_pc(world)
    _, tl = world.employee(hr)
    world.assign(tl, "TL")
    _, pm = world.employee(hr)
    world.assign(pm, "PM")
    _, ex = world.employee(hr)
    world.assign(ex, "EXECUTIVE")
    world.app.state.provisioner.users = [
        UserSummary(
            user_id=pc,
            display_name="Ravi PC",
            email="ravi.pc@example.com",
            status="ACTIVE",
            is_employee=True,
        )
    ]
    return emp, pc, tl, pm, ex


def punch(world, pc, came: datetime, left: datetime | None = None, where=None, **reasons):
    world.clock.set(came)
    extra = {"where": where} if where else {}
    r = send(world, pc, "in", token(world, pc), reason=reasons.get("why_in", "Traffic"), **extra)
    assert r.status_code == 200, r.text
    if left is not None:
        world.clock.set(left)
        r = send(
            world,
            pc,
            "out",
            token(world, pc, "CHECK_OUT"),
            reason=reasons.get("why_out", "Customer visit"),
            **extra,
        )
        assert r.status_code == 200, r.text


def adjust(world, user, day, **parts):
    return world.client.post(
        "/hr/v1/attendance/corrections",
        json={"workDate": day, "requested": "ADJUST", "reason": "The app did not open", **parts},
        headers=world.headers(user),
    )


def _day(world, emp, day):
    with world.engine.connect() as conn:
        return (
            conn.execute(
                text(
                    "SELECT check_in_at, check_out_at, check_in_flags, check_out_flags"
                    " FROM hr.attendance_day WHERE employee_id = CAST(:e AS uuid)"
                    " AND work_date = :d"
                ),
                {"e": emp["employeeId"], "d": day},
            )
            .mappings()
            .one()
        )


def _exceptions(world, emp) -> dict[tuple[str, str], str]:
    with world.engine.connect() as conn:
        return {
            (r[0], r[1]): r[2]
            for r in conn.execute(
                text(
                    "SELECT kind, event, status FROM hr.attendance_exception"
                    " WHERE employee_id = CAST(:e AS uuid)"
                ),
                {"e": emp["employeeId"]},
            )
        }


# ---- time and location corrections -----------------------------------------------------------


def test_an_earlier_check_in_once_approved_is_written_and_resolves_the_late(world):
    without_month_limits(world.engine)
    emp, pc, tl, _, _ = team(world)
    punch(world, pc, ist(2026, 10, 7, 11, 40), ist(2026, 10, 7, 19, 0))
    world.clock.set(ist(2026, 10, 9, 12, 0))
    asked = adjust(world, pc, "2026-10-07", checkInTime="10:20")
    assert asked.status_code == 200, asked.text
    assert asked.json()["requested"] == "ADJUST"
    assert datetime.fromisoformat(asked.json()["checkInAt"]) == ist(2026, 10, 7, 10, 20)
    assert _day(world, emp, "2026-10-07")["check_in_at"] == ist(2026, 10, 7, 11, 40)

    (item,) = world.client.get(
        "/hr/v1/approvals/attendance-corrections", headers=world.headers(tl)
    ).json()["items"]
    assert item["day"]["checkInAt"].startswith("2026-10-07T06:10")  # 11:40 IST as recorded
    assert decide(world, tl, item["correctionId"]).json()["status"] == "APPROVED"

    day = _day(world, emp, "2026-10-07")
    assert day["check_in_at"] == ist(2026, 10, 7, 10, 20)
    assert "APPROVED_CORRECTION" in day["check_in_flags"] and "LATE" not in day["check_in_flags"]
    assert _exceptions(world, emp)[("LATE_CHECK_IN", "CHECK_IN")] == "RESOLVED"
    with world.engine.connect() as conn:
        assert grace.late_early_in_month(conn, emp["employeeId"], date(2026, 10, 7)) == []
    (mine,) = world.client.get(
        "/hr/v1/attendance/corrections/me", headers=world.headers(pc)
    ).json()["items"]
    assert datetime.fromisoformat(mine["previousCheckInAt"]) == ist(2026, 10, 7, 11, 40)


def test_a_later_check_out_resolves_the_early_and_the_short_day(world):
    without_month_limits(world.engine)
    emp, pc, tl, _, _ = team(world)
    punch(world, pc, ist(2026, 10, 7, 10, 30), ist(2026, 10, 7, 14, 0))
    assert _exceptions(world, emp)[("SHORT_DAY", "CHECK_OUT")] == "PENDING"
    world.clock.set(ist(2026, 10, 9, 12, 0))
    cid = adjust(world, pc, "2026-10-07", checkOutTime="18:45").json()["correctionId"]
    assert decide(world, tl, cid).json()["status"] == "APPROVED"
    day = _day(world, emp, "2026-10-07")
    assert day["check_out_at"] == ist(2026, 10, 7, 18, 45)
    found = _exceptions(world, emp)
    assert found[("EARLY_CHECK_OUT", "CHECK_OUT")] == "RESOLVED"
    assert found[("SHORT_DAY", "CHECK_OUT")] == "RESOLVED"


def test_a_check_in_still_late_after_correction_keeps_the_late(world):
    without_month_limits(world.engine)
    emp, pc, tl, _, _ = team(world)
    punch(world, pc, ist(2026, 10, 7, 12, 30), ist(2026, 10, 7, 19, 0))
    world.clock.set(ist(2026, 10, 9, 12, 0))
    cid = adjust(world, pc, "2026-10-07", checkInTime="11:50").json()["correctionId"]
    decide(world, tl, cid)
    assert _day(world, emp, "2026-10-07")["check_in_at"] == ist(2026, 10, 7, 11, 50)
    assert _exceptions(world, emp)[("LATE_CHECK_IN", "CHECK_IN")] == "PENDING"


def test_a_location_correction_resolves_only_the_side_asked(world):
    without_month_limits(world.engine)
    emp, pc, tl, _, _ = team(world)
    punch(world, pc, ist(2026, 10, 7, 10, 25), ist(2026, 10, 7, 18, 40), where=FAR)
    found = _exceptions(world, emp)
    assert found[("OUT_OF_FENCE", "CHECK_IN")] == "PENDING"
    assert found[("OUT_OF_FENCE", "CHECK_OUT")] == "PENDING"
    world.clock.set(ist(2026, 10, 9, 12, 0))
    cid = adjust(world, pc, "2026-10-07", locationCheckIn=True).json()["correctionId"]
    decide(world, tl, cid)
    found = _exceptions(world, emp)
    assert found[("OUT_OF_FENCE", "CHECK_IN")] == "RESOLVED"
    assert found[("OUT_OF_FENCE", "CHECK_OUT")] == "PENDING"
    day = _day(world, emp, "2026-10-07")
    assert "LOCATION_CORRECTED" in day["check_in_flags"]
    assert "OUT_OF_FENCE" not in day["check_in_flags"]
    assert "OUT_OF_FENCE" in day["check_out_flags"]
    # A second time correction may follow an approved one; the check-out location now
    cid = adjust(world, pc, "2026-10-07", locationCheckOut=True).json()["correctionId"]
    assert decide(world, tl, cid).json()["status"] == "APPROVED"
    assert _exceptions(world, emp)[("OUT_OF_FENCE", "CHECK_OUT")] == "RESOLVED"


def test_a_missing_check_out_can_be_recorded_with_a_time_correction(world):
    without_month_limits(world.engine)
    emp, pc, tl, _, _ = team(world)
    punch(world, pc, ist(2026, 10, 7, 10, 25))
    world.clock.set(ist(2026, 10, 9, 12, 0))
    cid = adjust(world, pc, "2026-10-07", checkOutTime="18:40").json()["correctionId"]
    decide(world, tl, cid)
    day = _day(world, emp, "2026-10-07")
    assert day["check_out_at"] == ist(2026, 10, 7, 18, 40)
    assert day["check_out_flags"] == ["APPROVED_CORRECTION"]


def test_what_a_time_correction_cannot_ask_for(world):
    without_month_limits(world.engine)
    emp, pc, tl, _, _ = team(world)
    world.clock.set(ist(2026, 10, 9, 12, 0))
    no_day = adjust(world, pc, "2026-10-06", checkInTime="10:00")
    assert no_day.status_code == 422 and "no check-in" in no_day.json()["detail"]
    punch(world, pc, ist(2026, 10, 7, 10, 40), ist(2026, 10, 7, 18, 40))
    world.clock.set(ist(2026, 10, 9, 12, 0))

    def detail(**parts):
        r = adjust(world, pc, "2026-10-07", **parts)
        assert r.status_code == 422, r.text
        return r.json()["detail"]

    assert "Say what to correct" in detail()
    assert "before the recorded one (10:40)" in detail(checkInTime="10:50")
    assert "after the recorded one (18:40)" in detail(checkOutTime="18:00")
    assert "was not outside your outlet" in detail(locationCheckIn=True)
    assert "was not outside your outlet" in detail(locationCheckOut=True)
    world.clock.set(ist(2026, 11, 2, 12, 0))
    assert "Only days in November 2026" in detail(checkInTime="10:00")
    # a check-in time is not part of a full-day request
    world.clock.set(ist(2026, 10, 9, 12, 0))
    mixed = world.client.post(
        "/hr/v1/attendance/corrections",
        json={
            "workDate": "2026-10-07",
            "requested": "FULL_DAY",
            "checkInTime": "10:00",
            "reason": "x",
        },
        headers=world.headers(pc),
    )
    assert mixed.status_code == 422
    # one waiting request per day
    assert adjust(world, pc, "2026-10-07", checkInTime="10:10").status_code == 200
    assert adjust(world, pc, "2026-10-07", checkInTime="10:05").status_code == 409


def test_approving_after_the_day_changed_writes_nothing(world):
    without_month_limits(world.engine)
    emp, pc, tl, _, _ = team(world)
    punch(world, pc, ist(2026, 10, 9, 10, 25))
    world.clock.set(ist(2026, 10, 9, 13, 0))
    cid = adjust(world, pc, "2026-10-09", checkOutTime="12:50").json()["correctionId"]
    world.clock.set(ist(2026, 10, 9, 18, 30))
    assert send(world, pc, "out", token(world, pc, "CHECK_OUT")).status_code == 200
    assert decide(world, tl, cid).status_code == 409
    assert _day(world, emp, "2026-10-09")["check_out_at"] == ist(2026, 10, 9, 18, 30)


def test_the_calendar_counts_an_approved_time_correction_from_the_day(world):
    without_month_limits(world.engine)
    emp, pc, tl, _, _ = team(world)
    punch(world, pc, ist(2026, 10, 7, 11, 40), ist(2026, 10, 7, 19, 0))
    world.clock.set(ist(2026, 10, 9, 12, 0))

    def status():
        cal = world.client.get(
            "/hr/v1/attendance/calendar/me", params={"month": "2026-10"}, headers=world.headers(pc)
        ).json()
        return next(d for w in cal["weeks"] for d in w["days"] if d["date"] == "2026-10-07")

    assert status()["status"] == "PENDING_APPROVAL"
    cid = adjust(world, pc, "2026-10-07", checkInTime="10:20").json()["correctionId"]
    decide(world, tl, cid)
    assert status()["status"] == "PRESENT"


# ---- the month's figures and limits ---------------------------------------------------------


def _late_days(world, pc, days=(1, 2, 3, 5, 6, 7)):
    """Six days in October 2026 checked in at 12:30 (2 hours after 10:30) and out at 19:45."""
    for d in days:
        punch(world, pc, ist(2026, 10, d, 12, 30), ist(2026, 10, d, 19, 45))


def test_month_figures_count_late_and_early_time(world):
    emp, pc, tl, _, _ = team(world)
    punch(world, pc, ist(2026, 10, 1, 11, 30), ist(2026, 10, 1, 19, 30))  # 1 h late
    punch(world, pc, ist(2026, 10, 2, 10, 30), ist(2026, 10, 2, 16, 30))  # 2 h early
    punch(world, pc, ist(2026, 10, 3, 11, 0), ist(2026, 10, 3, 18, 30))  # not late (before 11:15)
    with world.engine.connect() as conn:
        stats = month_stats(conn, emp["employeeId"], date(2026, 10, 1), ist(2026, 10, 9, 12, 0))
    view = stats.view()
    assert (view["lateCheckIns"], view["lateMinutes"]) == (1, 60)
    assert (view["earlyCheckOuts"], view["earlyMinutes"]) == (1, 120)
    assert view["lateEarlyMinutes"] == 180
    assert view["daysClocked"] == 3
    assert view["averageMinutes"] == round((480 + 360 + 450) / 3)
    assert view["expectedHours"] == 9 and view["escalated"] is False
    cal = world.client.get(
        "/hr/v1/attendance/calendar/me", params={"month": "2026-10"}, headers=world.headers(pc)
    ).json()
    assert cal["monthStats"]["lateMinutes"] == 60


def test_more_than_ten_hours_late_or_early_sends_requests_to_the_pm_or_executive(world):
    emp, pc, tl, pm, ex = team(world)
    _late_days(world, pc, (1, 2, 3, 5, 6))  # 10 hours: not more than 10
    with world.engine.connect() as conn:
        assert not month_stats(conn, emp["employeeId"], date(2026, 10, 1), world.clock()).escalated
    assert world.mailer.sent == []
    assert world.client.get("/hr/v1/approvals/attendance", headers=world.headers(tl)).json()[
        "items"
    ]
    _late_days(world, pc, (7,))  # 12 hours
    with world.engine.connect() as conn:
        stats = month_stats(conn, emp["employeeId"], date(2026, 10, 1), world.clock())
    assert stats.reasons == ["LATE_EARLY_HOURS"]

    # The Team Lead no longer sees or decides them; the PM and the Executive do, with the note.
    lead = world.client.get("/hr/v1/approvals/attendance", headers=world.headers(tl)).json()
    assert lead["items"] == []
    items = world.client.get("/hr/v1/approvals/attendance", headers=world.headers(pm)).json()[
        "items"
    ]
    assert len(items) == 6
    note = items[0]["approvalNote"]
    assert note.startswith("Only a Project Manager or Executive can approve this")
    assert "12 h 00 m in October 2026 (more than 10 hours)" in note
    assert items[0]["monthStats"]["lateCheckIns"] == 6
    xid = items[0]["exceptionId"]
    by_tl = world.client.post(
        f"/hr/v1/approvals/attendance/{xid}/decision",
        json={"decision": "APPROVE"},
        headers=world.headers(tl),
    )
    assert by_tl.status_code == 404
    by_ex = world.client.post(
        f"/hr/v1/approvals/attendance/{xid}/decision",
        json={"decision": "APPROVE"},
        headers=world.headers(ex),
    )
    assert by_ex.json()["status"] == "APPROVED"

    # The PC was told once, by email.
    (mail,) = world.mailer.sent
    assert mail["to"] == "ravi.pc@example.com"
    assert "Project Manager or Executive" in mail["body"] and "October 2026" in mail["subject"]
    _late_days(world, pc, (8,))
    assert len(world.mailer.sent) == 1

    # A correction raised now carries the same note, for the PC and the approver.
    world.clock.set(ist(2026, 10, 9, 12, 0))
    asked = adjust(world, pc, "2026-10-08", checkInTime="10:30")
    assert asked.json()["approvalNote"] == note.replace("12 h 00 m", "14 h 00 m")
    (mine,) = world.client.get(
        "/hr/v1/attendance/corrections/me", headers=world.headers(pc)
    ).json()["items"]
    assert mine["approvalNote"].startswith("Only a Project Manager or Executive")
    assert (
        world.client.get(
            "/hr/v1/approvals/attendance-corrections", headers=world.headers(tl)
        ).json()["items"]
        == []
    )
    (c,) = world.client.get(
        "/hr/v1/approvals/attendance-corrections", headers=world.headers(pm)
    ).json()["items"]
    assert decide(world, tl, c["correctionId"]).status_code == 404
    assert decide(world, pm, c["correctionId"]).json()["status"] == "APPROVED"


def test_a_low_average_alone_changes_nothing(world):
    # Product owner, 16 Nov 2026: "remove average rule". The average is shown, not enforced.
    emp, pc, tl, pm, _ = team(world)
    for d in (5, 6):
        punch(world, pc, ist(2026, 10, d, 10, 30), ist(2026, 10, d, 17, 0))  # 6 h 30 m
    with world.engine.connect() as conn:
        stats = month_stats(conn, emp["employeeId"], date(2026, 10, 1), world.clock())
    assert stats.view()["averageMinutes"] == 390
    assert stats.reasons == [] and stats.note() is None
    world.clock.set(ist(2026, 10, 9, 12, 0))
    assert adjust(world, pc, "2026-10-06", checkOutTime="18:30").json()["approvalNote"] is None
    (item,) = world.client.get(
        "/hr/v1/approvals/attendance-corrections", headers=world.headers(tl)
    ).json()["items"]
    assert decide(world, tl, item["correctionId"]).json()["status"] == "APPROVED"
    assert world.mailer.sent == []


def test_a_team_leads_own_requests_go_to_the_pm_or_executive(world):
    without_month_limits(world.engine)
    emp, pc, tl, pm, ex = team(world)
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    lead_emp, lead = world.employee(hr)
    world.assign(lead, "TL")
    punch(world, lead, ist(2026, 10, 7, 11, 40), ist(2026, 10, 7, 19, 0))
    world.clock.set(ist(2026, 10, 9, 12, 0))
    asked = adjust(world, lead, "2026-10-07", checkInTime="10:20")
    assert asked.json()["approvalNote"] == TEAM_LEAD_NOTE

    # The other Team Lead sees neither the late check-in nor the correction, and cannot decide them.
    for path in ("/hr/v1/approvals/attendance", "/hr/v1/approvals/attendance-corrections"):
        assert world.client.get(path, headers=world.headers(tl)).json()["items"] == []
    (late,) = world.client.get("/hr/v1/approvals/attendance", headers=world.headers(pm)).json()[
        "items"
    ]
    assert late["approvalNote"] == TEAM_LEAD_NOTE
    by_tl = world.client.post(
        f"/hr/v1/approvals/attendance/{late['exceptionId']}/decision",
        json={"decision": "APPROVE"},
        headers=world.headers(tl),
    )
    assert by_tl.status_code == 404
    (c,) = world.client.get(
        "/hr/v1/approvals/attendance-corrections", headers=world.headers(ex)
    ).json()["items"]
    assert decide(world, tl, c["correctionId"]).status_code == 404
    assert decide(world, ex, c["correctionId"]).json()["status"] == "APPROVED"
    # The PC's requests still go to the Team Lead.
    punch(world, pc, ist(2026, 10, 8, 11, 40), ist(2026, 10, 8, 19, 0))
    assert world.client.get("/hr/v1/approvals/attendance", headers=world.headers(tl)).json()[
        "items"
    ]
    # A Team Lead's own request never counts against a Team Lead at the Monday deadline.
    with world.engine.begin() as conn:
        conn.execute(
            text("UPDATE hr.attendance_exception SET created_at = '2026-10-08T12:00:00+05:30'")
        )
    with world.engine.connect() as conn:
        waiting = overdue_team_leads(conn, datetime(2026, 10, 19, 9, 0, tzinfo=IST))
    assert [i["employeeName"] for i in waiting[tl]] == [emp["fullName"]]


def test_escalated_requests_never_count_against_a_team_lead_at_the_deadline(world):
    emp, pc, tl, pm, _ = team(world)
    _late_days(world, pc)
    with world.engine.begin() as conn:
        conn.execute(
            text("UPDATE hr.attendance_exception SET created_at = '2026-10-08T12:00:00+05:30'")
        )
    monday = datetime(2026, 10, 19, 9, 0, tzinfo=IST)
    with world.engine.connect() as conn:
        assert tl not in overdue_team_leads(conn, monday)
    without_month_limits(world.engine)
    with world.engine.connect() as conn:
        assert len(overdue_team_leads(conn, monday)[tl]) == 6
