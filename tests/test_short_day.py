"""Less than the minimum hours in a day goes to the Team Lead, and the Project Manager is told
(product owner, 6 Nov 2026)."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text

from hrmgmt import attendance_grace as grace
from hrmgmt import permissions as perm
from hrmgmt.mailer import MailError
from hrmgmt.provisioning import UserSummary
from tests.support import World, ist
from tests.test_attendance import send, setup_pc, token


class FakeMailer:
    def __init__(self):
        self.sent: list[dict] = []
        self.error: str | None = None

    def send(self, *, to, subject, body, **_):
        if self.error:
            raise MailError(self.error, "down")
        self.sent.append({"to": to, "subject": subject, "body": body})


@pytest.fixture()
def world(migrated_engine):
    w = World(migrated_engine)
    w.clean_assignments()
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM hr.attendance_exception"))
        conn.execute(text("DELETE FROM hr.attendance_day"))
        conn.execute(text("DELETE FROM hr.capture_token"))
        conn.execute(text("DELETE FROM hr.holiday WHERE status = 'DECLARED'"))
        conn.execute(text("DELETE FROM hr.setting"))
        conn.execute(text("DELETE FROM hr.message_log"))
    w.mailer = FakeMailer()
    w.app.state.mailer = w.mailer
    return w


def team(world: World):
    """A PC on project P1 with its Team Lead and Project Manager (who has an email in Security)."""
    emp, pc, hr = setup_pc(world)
    _, tl = world.employee(hr)
    world.assign(tl, "TL")
    _, pm = world.employee(hr)
    world.assign(pm, "PM")
    world.app.state.provisioner.users = [
        UserSummary(
            user_id=pm,
            display_name="Priya PM",
            email="priya.pm@example.com",
            status="ACTIVE",
            is_employee=True,
        )
    ]
    return emp, pc, tl, pm


def day(world: World, pc: str, came, left, reason: str | None = "Doctor visit"):
    world.clock.set(came)
    r = send(world, pc, "in", token(world, pc), reason="Bus late")
    assert r.status_code == 200, r.text
    world.clock.set(left)
    extra = {"reason": reason} if reason else {}
    return send(world, pc, "out", token(world, pc, "CHECK_OUT"), **extra)


def test_under_five_hours_needs_a_reason_and_goes_to_the_team_lead(world):
    _, pc, tl, pm = team(world)
    world.clock.set(ist(2026, 10, 5, 13, 0))
    assert send(world, pc, "in", token(world, pc), reason="Bank work").status_code == 200
    world.clock.set(ist(2026, 10, 5, 17, 40))  # 4 h 40 m, and not an early check-out
    tok = token(world, pc, "CHECK_OUT")
    first = send(world, pc, "out", tok)
    assert first.status_code == 422 and first.json()["code"] == "ATTENDANCE_REASON_REQUIRED"
    assert "checked in for 4 h 40 m today; the minimum is 5 hours" in first.json()["detail"]
    assert world.mailer.sent == []
    out = send(world, pc, "out", tok, reason="Bank work ran long")
    assert out.status_code == 200, out.text
    assert out.json()["needsApproval"] == ["SHORT_DAY"] and "SHORT_DAY" in out.json()["flags"]
    items = world.client.get("/hr/v1/approvals/attendance", headers=world.headers(tl)).json()[
        "items"
    ]
    short = [i for i in items if i["kind"] == "SHORT_DAY"]
    assert len(short) == 1 and short[0]["reason"] == "Bank work ran long"
    assert short[0]["event"] == "CHECK_OUT"
    today = world.client.get("/hr/v1/attendance/today", headers=world.headers(pc)).json()
    assert today["day"]["status"] == "PENDING_APPROVAL"


def test_the_project_manager_is_emailed_and_it_is_logged(world):
    _, pc, _, pm = team(world)
    out = day(world, pc, ist(2026, 10, 5, 13, 0), ist(2026, 10, 5, 16, 30))
    assert out.status_code == 200 and "SHORT_DAY" in out.json()["needsApproval"]
    (mail,) = world.mailer.sent
    assert mail["to"] == "priya.pm@example.com"
    assert "Short day:" in mail["subject"] and "3 h 30 m" in mail["subject"]
    assert "Dear Priya PM," in mail["body"] and "Reason given: Doctor visit" in mail["body"]
    assert "check-in 13:00, check-out 16:30 IST" in mail["body"]
    assert "gone to the Team Lead" in mail["body"]
    with world.engine.connect() as conn:
        logged = conn.execute(
            text("SELECT user_id, template_code, status, sent_by FROM hr.message_log")
        ).all()
    assert [(str(u), t, s, b) for u, t, s, b in logged] == [(pm, "SHORT_DAY_PM", "SENT", "SYSTEM")]


def test_five_hours_or_more_is_not_a_short_day(world):
    _, pc, _, _ = team(world)
    out = day(world, pc, ist(2026, 10, 5, 12, 0), ist(2026, 10, 5, 17, 0), reason=None)
    assert out.status_code == 200 and "SHORT_DAY" not in out.json()["needsApproval"]
    assert world.mailer.sent == []


def test_the_minimum_is_a_setting(world):
    _, pc, _, _ = team(world)
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hr.setting (key, value) VALUES ('attendance.min_work_hours', '4')"
                " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
            )
        )
    out = day(world, pc, ist(2026, 10, 5, 13, 0), ist(2026, 10, 5, 17, 30), reason=None)
    assert out.status_code == 200 and "SHORT_DAY" not in out.json()["needsApproval"]


def test_an_off_day_or_approved_half_day_leave_is_not_a_short_day(world):
    emp, pc, _, _ = team(world)
    out = day(world, pc, ist(2026, 10, 4, 10, 0), ist(2026, 10, 4, 12, 0), reason=None)  # Sunday
    assert out.status_code == 200 and out.json()["needsApproval"] == []
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hr.leave_request (employee_id, leave_type, from_date, to_date, days,"
                " half_day, reason, status, approver_rule) VALUES (CAST(:e AS uuid), 'SICK',"
                " '2026-10-05', '2026-10-05', 0.5, true, 'Half day', 'APPROVED', 'HR')"
            ),
            {"e": emp["employeeId"]},
        )
    out = day(world, pc, ist(2026, 10, 5, 10, 20), ist(2026, 10, 5, 14, 30), reason="Leave")
    assert out.status_code == 200 and "SHORT_DAY" not in out.json()["needsApproval"]
    assert world.mailer.sent == []


def test_a_rejected_short_day_is_a_half_day(world):
    emp, pc, tl, _ = team(world)
    day(world, pc, ist(2026, 10, 5, 13, 0), ist(2026, 10, 5, 17, 30))
    items = world.client.get("/hr/v1/approvals/attendance", headers=world.headers(tl)).json()[
        "items"
    ]
    (short,) = [i for i in items if i["kind"] == "SHORT_DAY"]
    r = world.client.post(
        f"/hr/v1/approvals/attendance/{short['exceptionId']}/decision",
        json={"decision": "REJECT", "note": "Not agreed in advance"},
        headers=world.headers(tl),
    )
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"
    with world.engine.connect() as conn:
        assert grace.half_days(conn, emp["employeeId"], date(2026, 10, 1)) == 0.5


def test_check_out_works_when_email_fails_or_there_is_no_project_manager(world):
    _, pc, _, pm = team(world)
    world.mailer.error = "MAIL_UNAVAILABLE"
    out = day(world, pc, ist(2026, 10, 5, 13, 0), ist(2026, 10, 5, 16, 0))
    assert out.status_code == 200 and "SHORT_DAY" in out.json()["needsApproval"]
    with world.engine.connect() as conn:
        assert conn.execute(text("SELECT status, reason_code FROM hr.message_log")).all() == [
            ("FAILED", "MAIL_UNAVAILABLE")
        ]
    # a Team Lead with no Project Manager on the project: the request still waits, nobody is mailed
    world.mailer.error = None
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    _, lone = world.employee(hr)
    world.assign(lone, "PC", tenant="tenant-b", outlet=("Puri Motors", 19.81, 85.83))
    world.clock.set(ist(2026, 10, 6, 13, 0))
    assert (
        send(world, lone, "in", token(world, lone), where=(19.81, 85.83), reason="x").status_code
        == 200
    )
    world.clock.set(ist(2026, 10, 6, 15, 0))
    out = send(
        world, lone, "out", token(world, lone, "CHECK_OUT"), where=(19.81, 85.83), reason="Ill"
    )
    assert out.status_code == 200 and "SHORT_DAY" in out.json()["needsApproval"]
    assert world.mailer.sent == []


def test_the_daily_view_labels_a_short_day(world):
    from hrmgmt.attendance_report import delinquency_label

    assert delinquency_label({"code": "SHORT_DAY"}) == "Fewer than the minimum hours"
