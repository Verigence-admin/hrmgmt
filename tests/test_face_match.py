"""Does the face in a check-in or check-out photo match the employee? Only ever a flag for HR."""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageEnhance
from sqlalchemy import text

from hrmgmt import permissions as perm
from hrmgmt.facematch import MODEL_ID, embed, similarity
from tests.support import NEAR, OUTLET, World, ist, jpeg
from tests.test_attendance import token

_FACE_FILE = Path(__file__).parent / "fixtures" / "face_public_domain.jpg"
FACE = _FACE_FILE.read_bytes()


def numbers(index: int) -> bytes:
    """Face numbers that point their own way: any two different indexes are a complete mismatch."""
    return np.eye(128, dtype=np.float32)[index].tobytes()


@pytest.fixture()
def world(migrated_engine):
    w = World(migrated_engine, now=ist(2026, 10, 5, 10, 0))
    w.clean_assignments()
    return w


def person(world):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE, perm.HR_EMPLOYEE_READ)
    emp, user = world.employee(hr)
    world.assign(user, "PC", outlet=OUTLET)
    return hr, emp, user


def keeper(world):
    return world.grant(str(uuid.uuid4()), perm.HR_ATTENDANCE_READ_ALL)


def set_profile_photo(world, hr, emp, data=FACE):
    r = world.client.post(
        f"/hr/v1/employees/{emp['employeeId']}/photo",
        files={"file": ("me.jpg", data, "image/jpeg")},
        headers=world.headers(hr),
    )
    assert r.status_code == 200, r.text


def punch(world, user, event, photo=FACE):
    purpose = "CHECK_IN" if event == "in" else "CHECK_OUT"
    path = "/hr/v1/attendance/check-in" if event == "in" else "/hr/v1/attendance/check-out"
    r = world.client.post(
        path,
        headers=world.headers(user),
        data={
            "token": token(world, user, purpose),
            "latitude": str(NEAR[0]),
            "longitude": str(NEAR[1]),
            "accuracy_m": "20",
            "position_age_s": "3",
        },
        files={"photo": ("p.jpg", photo, "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    return r.json()


def day(world, emp):
    with world.engine.connect() as conn:
        return (
            conn.execute(
                text(
                    "SELECT check_in_face IS NOT NULL AS has_face, check_in_face_score,"
                    " check_in_face_ref, check_out_face_score, check_out_face_ref"
                    " FROM hr.attendance_day WHERE employee_id = CAST(:e AS uuid)"
                ),
                {"e": emp["employeeId"]},
            )
            .mappings()
            .one()
        )


def delinquencies(world, emp):
    rows = world.client.get(
        "/hr/v1/attendance/daily",
        params={"date": "2026-10-05"},
        headers=world.headers(keeper(world)),
    ).json()["rows"]
    mine = next(r for r in rows if r["employeeCode"] == emp["employeeCode"])
    return [(d["code"], d["label"], d["side"]) for d in mine["delinquencies"]]


# ---- the matching itself ---------------------------------------------------------------------


def test_a_face_becomes_128_numbers_and_the_same_face_matches_itself():
    face = Image.open(_FACE_FILE).convert("RGB")
    mine = embed(face)
    assert mine is not None and len(mine) == 128 * 4
    assert similarity(mine, mine) == 1.0
    for other in (
        ImageEnhance.Brightness(face).enhance(0.6),
        face.rotate(8, fillcolor=(128, 128, 128)),
        face.resize((face.width // 2, face.height // 2)),
    ):
        assert similarity(mine, embed(other)) > 0.6  # well above the 0.363 cut-off


def test_the_score_is_the_models_own_cosine_score():
    import cv2

    face = Image.open(_FACE_FILE).convert("RGB")
    a, b = embed(face), embed(face.rotate(10, fillcolor=(128, 128, 128)))
    recognizer = cv2.FaceRecognizerSF.create(
        str(
            Path(__file__).parents[1]
            / "src/hrmgmt/face_models/face_recognition_sface_2021dec_int8.onnx"
        ),
        "",
    )
    theirs = recognizer.match(
        np.frombuffer(a, np.float32).reshape(1, -1),
        np.frombuffer(b, np.float32).reshape(1, -1),
        cv2.FaceRecognizerSF_FR_COSINE,
    )
    assert similarity(a, b) == pytest.approx(theirs, abs=0.001)


def test_no_face_gives_no_numbers_and_unlike_numbers_score_low():
    assert embed(Image.new("RGB", (640, 480), (120, 160, 200))) is None
    assert similarity(numbers(1), numbers(2)) == 0.0
    assert similarity(b"", b"") is None and similarity(numbers(1), b"\x00\x00\x00\x00") is None


def test_if_the_models_are_missing_nothing_is_ever_compared(monkeypatch):
    from hrmgmt import facematch

    monkeypatch.setattr(facematch, "_ready", lambda: False)
    assert embed(Image.open(_FACE_FILE).convert("RGB")) is None


# ---- with a profile photo --------------------------------------------------------------------


def test_the_profile_photo_is_the_reference_for_check_in_and_check_out(world):
    hr, emp, user = person(world)
    set_profile_photo(world, hr, emp)
    with world.engine.connect() as conn:
        stored = conn.execute(
            text(
                "SELECT model, embedding IS NOT NULL FROM hr.employee_face WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": emp["employeeId"]},
        ).one()
    assert stored == (MODEL_ID, True)
    assert punch(world, user, "in")["flags"] == []
    world.clock.set(ist(2026, 10, 5, 18, 30))
    assert punch(world, user, "out")["flags"] == []
    seen = day(world, emp)
    assert seen["check_in_face_ref"] == "PROFILE" and float(seen["check_in_face_score"]) > 0.9
    assert seen["check_out_face_ref"] == "PROFILE" and float(seen["check_out_face_score"]) > 0.9
    assert delinquencies(world, emp) == []


def test_a_different_face_at_check_in_or_check_out_is_flagged_not_refused(world, monkeypatch):
    hr, emp, user = person(world)
    set_profile_photo(world, hr, emp)
    monkeypatch.setattr("hrmgmt.api.attendance.embed", lambda image: numbers(7))
    sent = punch(world, user, "in")
    assert sent["flags"] == ["FACE_MISMATCH"] and sent["needsApproval"] == []
    world.clock.set(ist(2026, 10, 5, 18, 30))
    assert punch(world, user, "out")["flags"] == ["FACE_MISMATCH"]
    assert delinquencies(world, emp) == [
        ("FACE_MISMATCH", "Face does not match the profile photo at check-in", "CHECK_IN"),
        ("FACE_MISMATCH", "Face does not match the profile photo at check-out", "CHECK_OUT"),
    ]
    assert float(day(world, emp)["check_in_face_score"]) < 0.363


def test_a_photo_set_before_face_matching_existed_is_worked_out_the_first_time(world):
    hr, emp, user = person(world)
    set_profile_photo(world, hr, emp)
    with world.engine.begin() as conn:  # as if the photo had been set by the older version
        conn.execute(
            text("DELETE FROM hr.employee_face WHERE employee_id = CAST(:e AS uuid)"),
            {"e": emp["employeeId"]},
        )
    assert punch(world, user, "in")["flags"] == []
    assert day(world, emp)["check_in_face_ref"] == "PROFILE"
    with world.engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM hr.employee_face WHERE employee_id = CAST(:e AS uuid)"),
                {"e": emp["employeeId"]},
            ).scalar_one()
            == 1
        )


def test_a_new_profile_photo_replaces_the_old_numbers(world, monkeypatch):
    hr, emp, user = person(world)
    monkeypatch.setattr("hrmgmt.api.employees.embed", lambda image: numbers(3))
    set_profile_photo(world, hr, emp)
    monkeypatch.setattr("hrmgmt.api.attendance.embed", lambda image: numbers(3))
    assert punch(world, user, "in")["flags"] == []  # the same numbers as the profile photo
    monkeypatch.setattr("hrmgmt.api.employees.embed", lambda image: numbers(9))
    set_profile_photo(world, hr, emp)
    world.clock.set(ist(2026, 10, 5, 18, 30))
    assert punch(world, user, "out")["flags"] == [
        "FACE_MISMATCH"
    ]  # now compared with the new photo


# ---- without a profile photo -----------------------------------------------------------------


def test_without_a_profile_photo_the_check_out_is_compared_with_the_check_in(world):
    _, emp, user = person(world)
    assert punch(world, user, "in")["flags"] == []  # nothing to compare with yet
    assert day(world, emp)["has_face"] is True and day(world, emp)["check_in_face_score"] is None
    world.clock.set(ist(2026, 10, 5, 18, 30))
    assert punch(world, user, "out")["flags"] == []
    seen = day(world, emp)
    assert seen["check_out_face_ref"] == "CHECK_IN" and float(seen["check_out_face_score"]) > 0.9


def test_a_different_person_at_check_out_than_at_check_in_is_flagged_with_that_wording(
    world, monkeypatch
):
    _, emp, user = person(world)
    monkeypatch.setattr("hrmgmt.api.attendance.embed", lambda image: numbers(1))
    punch(world, user, "in")
    monkeypatch.setattr("hrmgmt.api.attendance.embed", lambda image: numbers(2))
    world.clock.set(ist(2026, 10, 5, 18, 30))
    assert punch(world, user, "out")["flags"] == ["FACE_MISMATCH"]
    assert delinquencies(world, emp) == [
        ("FACE_MISMATCH", "Face at check-out does not match the check-in photo", "CHECK_OUT")
    ]


def test_a_profile_photo_without_a_clear_face_falls_back_to_the_check_in_photo(world):
    hr, emp, user = person(world)
    set_profile_photo(world, hr, emp, data=jpeg())  # a plain colour block: no face in it
    with world.engine.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT embedding IS NULL FROM hr.employee_face WHERE employee_id = CAST(:e AS uuid)"
                ),
                {"e": emp["employeeId"]},
            ).scalar_one()
            is True
        )
    punch(world, user, "in")
    world.clock.set(ist(2026, 10, 5, 18, 30))
    punch(world, user, "out")
    assert day(world, emp)["check_out_face_ref"] == "CHECK_IN"


# ---- never in the way ------------------------------------------------------------------------


def test_a_photo_with_no_face_is_only_no_face_never_a_mismatch(world, monkeypatch):
    hr, emp, user = person(world)
    set_profile_photo(world, hr, emp)
    monkeypatch.setattr("hrmgmt.api.attendance.face_present", lambda image: False)
    assert punch(world, user, "in")["flags"] == ["NO_FACE"]
    assert day(world, emp)["check_in_face_score"] is None


def test_hr_can_switch_it_off_and_nothing_is_compared_or_kept(world, monkeypatch):
    hr, emp, user = person(world)
    set_profile_photo(world, hr, emp)
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hr.setting (key, value) VALUES ('attendance.face_match_on', '0')"
                " ON CONFLICT (key) DO UPDATE SET value = '0'"
            )
        )
    try:
        monkeypatch.setattr("hrmgmt.api.attendance.embed", lambda image: numbers(5))
        assert punch(world, user, "in")["flags"] == []
        assert day(world, emp)["has_face"] is False
    finally:
        with world.engine.begin() as conn:
            conn.execute(text("DELETE FROM hr.setting WHERE key = 'attendance.face_match_on'"))


def test_a_stricter_cut_off_flags_a_weaker_match(world, monkeypatch):
    hr, emp, user = person(world)
    set_profile_photo(world, hr, emp)
    face = Image.open(_FACE_FILE).convert("RGB").rotate(-20, fillcolor=(128, 128, 128))
    out = io.BytesIO()
    face.save(out, format="JPEG")
    tilted = out.getvalue()
    score = similarity(embed(Image.open(_FACE_FILE).convert("RGB")), embed(face))
    assert score is not None and 0.4 < score < 0.95
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hr.setting (key, value) VALUES ('attendance.face_match_threshold', :v)"
                " ON CONFLICT (key) DO UPDATE SET value = :v"
            ),
            {"v": str(int(score * 1000) + 20)},
        )
    try:
        assert punch(world, user, "in", photo=tilted)["flags"] == ["FACE_MISMATCH"]
    finally:
        with world.engine.begin() as conn:
            conn.execute(
                text("DELETE FROM hr.setting WHERE key = 'attendance.face_match_threshold'")
            )


def test_the_face_numbers_go_with_the_employee_when_they_are_deleted(world):
    hr, emp, _ = person(world)
    set_profile_photo(world, hr, emp)
    keeper_user = world.grant(str(uuid.uuid4()), perm.HR_HOUSEKEEPING_MANAGE)
    r = world.client.post(
        "/hr/v1/housekeeping/employee/delete",
        json={"employee_code": emp["employeeCode"], "confirm": "DELETE"},
        headers=world.headers(keeper_user),
    )
    assert r.status_code == 200, r.text
    with world.engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM hr.employee_face WHERE employee_id = CAST(:e AS uuid)"),
                {"e": emp["employeeId"]},
            ).scalar_one()
            == 0
        )
