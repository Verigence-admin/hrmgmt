from __future__ import annotations

import uuid

from PIL import Image
from sqlalchemy import text

from hrmgmt import permissions as perm
from hrmgmt.facecheck import face_present
from tests.support import NEAR, OUTLET, World, ist
from tests.test_attendance import send, token


def test_a_plain_picture_has_no_face():
    assert face_present(Image.new("RGB", (640, 480), (120, 160, 200))) is False


def test_a_photo_without_a_face_is_accepted_but_flagged_for_hr(migrated_engine, monkeypatch):
    world = World(migrated_engine, now=ist(2026, 10, 5, 10, 0))
    world.clean_assignments()
    with migrated_engine.begin() as conn:
        for table in ("attendance_exception", "attendance_day", "capture_token"):
            conn.execute(text(f"DELETE FROM hr.{table}"))
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    keeper = world.grant(str(uuid.uuid4()), perm.HR_ATTENDANCE_READ_ALL)
    emp, user = world.employee(hr)
    world.assign(user, "PC", outlet=OUTLET)
    monkeypatch.setattr("hrmgmt.api.attendance.face_present", lambda image: False)
    sent = send(world, user, "in", token(world, user), where=NEAR)
    assert sent.status_code == 200, sent.text
    assert sent.json()["flags"] == ["NO_FACE"] and sent.json()["needsApproval"] == []
    daily = world.client.get(
        "/hr/v1/attendance/daily", params={"date": "2026-10-05"}, headers=world.headers(keeper)
    ).json()
    mine = [r for r in daily["rows"] if r["employeeCode"] == emp["employeeCode"]][0]
    assert [d["code"] for d in mine["delinquencies"]] == ["NO_FACE"]
    assert mine["delinquencies"][0]["label"] == "No face found in the photo"
    assert mine["status"] == "CHECKED_IN"  # nothing waits for approval


def test_when_the_check_cannot_run_nothing_is_flagged(migrated_engine, monkeypatch):
    world = World(migrated_engine, now=ist(2026, 10, 5, 10, 0))
    world.clean_assignments()
    with migrated_engine.begin() as conn:
        for table in ("attendance_exception", "attendance_day", "capture_token"):
            conn.execute(text(f"DELETE FROM hr.{table}"))
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    _, user = world.employee(hr)
    world.assign(user, "PC", outlet=OUTLET)
    monkeypatch.setattr("hrmgmt.api.attendance.face_present", lambda image: None)
    sent = send(world, user, "in", token(world, user), where=NEAR)
    assert sent.status_code == 200 and sent.json()["flags"] == []
