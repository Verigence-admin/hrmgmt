"""The Monday 09:00 approvals deadline: Team Leads with requests still waiting on them are paused in
Security; SuperAdmin restores them from myHR Settings with a note (product owner, 4-5 Nov 2026)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import text

from hrmgmt import permissions as perm
from hrmgmt.approver_deadline import latest_deadline, run_deadline
from hrmgmt.timeutil import IST
from tests.support import ist
from tests.test_leave import apply, person
from tests.test_leave_earning import attend
from tests.test_leave_earning import world as world  # noqa: F401 - the fixture

MONDAY_9 = datetime(2026, 11, 30, 9, 0, tzinfo=IST)


def _raised(world, exception_id: str, when: str) -> None:
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.attendance_exception SET created_at = CAST(:w AS timestamptz)"
                " WHERE exception_id = CAST(:x AS uuid)"
            ),
            {"w": when, "x": exception_id},
        )


def _reset(world) -> None:
    with world.engine.begin() as conn:
        conn.execute(text("TRUNCATE hr.approver_pause, hr.approver_deadline_run"))


def test_the_latest_deadline_is_the_last_monday_0900_ist():
    assert latest_deadline(datetime(2026, 11, 30, 9, 0, tzinfo=IST)) == MONDAY_9
    assert latest_deadline(datetime(2026, 12, 6, 23, 0, tzinfo=IST)) == MONDAY_9
    assert latest_deadline(datetime(2026, 11, 30, 8, 59, tzinfo=IST)) == datetime(
        2026, 11, 23, 9, 0, tzinfo=IST
    )


def test_every_team_lead_with_a_late_request_is_paused_once_and_superadmin_restores(world):
    _reset(world)
    emp, pc = person(world, "PC")
    _, tl1 = person(world, "TL")
    _, tl2 = person(world, "TL")
    _, pm = person(world, "PM")
    _, other_tl = person(world, "TL", tenant="tenant-b")
    late = attend(world, emp["employeeId"], [date(2026, 11, 26)], exception="PENDING")
    _raised(world, late[0], "2026-11-26T11:30:00+05:30")  # week of 23 Nov: due 30 Nov 09:00
    apply(world, pc, from_date="2026-12-07")  # raised now (1 Dec): due 7 Dec, not late
    provisioner = world.app.state.provisioner

    assert run_deadline(world.engine, provisioner, now=ist(2026, 11, 30, 9, 5)) == 2
    assert sorted(provisioner.paused) == sorted([tl1, tl2])  # both Team Leads; not the PM
    assert pm not in provisioner.paused and other_tl not in provisioner.paused
    # the same Monday is never processed twice
    assert run_deadline(world.engine, provisioner, now=ist(2026, 11, 30, 9, 15)) is None
    assert len(provisioner.paused) == 2

    admin = world.grant(str(uuid.uuid4()), perm.HR_ACCESS_PAUSE_MANAGE)
    assert (
        world.client.get("/hr/v1/admin/approver-pauses", headers=world.headers(tl1)).status_code
        == 403
    )
    listed = world.client.get("/hr/v1/admin/approver-pauses", headers=world.headers(admin)).json()
    assert listed["activeCount"] == 2
    mine = next(i for i in listed["items"] if i["userId"] == tl1)
    assert mine["overdueCount"] == 1 and mine["pausedInSecurity"]
    assert mine["overdueItems"][0]["type"] == "ATTENDANCE"

    url = f"/hr/v1/admin/approver-pauses/{mine['pauseId']}/restore"
    assert (
        world.client.post(url, json={"note": ""}, headers=world.headers(admin)).status_code == 422
    )
    done = world.client.post(url, json={"note": "Cleared on call"}, headers=world.headers(admin))
    assert done.status_code == 200, done.text
    assert done.json()["restoreNote"] == "Cleared on call" and done.json()["restoredAt"]
    assert provisioner.restored == [(tl1, admin, "Cleared on call")]
    again = world.client.post(url, json={"note": "again"}, headers=world.headers(admin))
    assert again.status_code == 409
    listed = world.client.get("/hr/v1/admin/approver-pauses", headers=world.headers(admin)).json()
    assert listed["activeCount"] == 1


def test_nothing_late_pauses_nobody_and_a_security_refusal_is_kept_for_superadmin(world):
    _reset(world)
    emp, _ = person(world, "PC")
    _, tl = person(world, "TL")
    provisioner = world.app.state.provisioner
    assert run_deadline(world.engine, provisioner, now=ist(2026, 11, 30, 9, 5)) == 0
    assert not getattr(provisioner, "paused", [])

    late = attend(world, emp["employeeId"], [date(2026, 12, 1)], exception="PENDING")
    _raised(world, late[0], "2026-12-01T11:30:00+05:30")  # due 7 Dec 09:00
    provisioner.pause_error = "SECURITY_UNAVAILABLE"
    assert run_deadline(world.engine, provisioner, now=ist(2026, 12, 7, 9, 1)) == 1
    with world.engine.begin() as conn:
        row = conn.execute(
            text("SELECT security_paused, security_error FROM hr.approver_pause")
        ).one()
    assert row[0] is False and "SECURITY_UNAVAILABLE" in row[1]
