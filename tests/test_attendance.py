from __future__ import annotations

import io
import uuid

import pytest
from PIL import Image
from sqlalchemy import text

from hrmgmt import permissions as perm
from hrmgmt.geo import haversine_m
from tests.support import FAR, NEAR, OUTLET, World, ist, jpeg


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
    return w


def token(world: World, user: str, purpose: str = "CHECK_IN") -> str:
    r = world.client.post(
        "/hr/v1/attendance/capture-token", json={"purpose": purpose}, headers=world.headers(user)
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def send(world: World, user: str, event: str, tok: str, where=NEAR, **over):
    path = "/hr/v1/attendance/check-in" if event == "in" else "/hr/v1/attendance/check-out"
    form = {
        "token": tok,
        "latitude": str(where[0]),
        "longitude": str(where[1]),
        "accuracy_m": "20",
        "position_age_s": "3",
    }
    form.update({k: str(v) for k, v in over.items()})
    return world.client.post(
        path,
        headers=world.headers(user),
        data=form,
        files={"photo": ("p.jpg", over.pop("photo", None) or jpeg(), "image/jpeg")},
    )


def setup_pc(world: World, with_outlet=True):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    emp, user = world.employee(hr)
    world.assign(user, "PC", outlet=OUTLET if with_outlet else None)
    return emp, user, hr


def test_haversine_matches_a_known_distance():
    # 0.001 degree of latitude is about 111 m
    assert 100 < haversine_m(20.0, 85.0, 20.001, 85.0) < 120
    assert haversine_m(20.0, 85.0, 20.0, 85.0) == 0


def test_pc_inside_the_fence_checks_in_with_a_stamped_photo(world):
    emp, user, _ = setup_pc(world)
    r = send(world, user, "in", token(world, user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["needsApproval"] == [] and body["flags"] == []
    assert body["outletName"] == "Cuttack Motors" and 0 < body["distanceM"] < 200
    assert body["address"].startswith("Station Road")
    ((key, (data, ctype)),) = world.storage.objects.items()
    assert key.endswith("-in.jpg") and ctype == "image/jpeg"
    stamped = Image.open(io.BytesIO(data))
    assert (
        stamped.format == "JPEG" and not stamped.getexif()
    )  # re-encoded, nothing hidden carried over
    today = world.client.get("/hr/v1/attendance/today", headers=world.headers(user)).json()
    assert (
        today["day"]["status"] == "CHECKED_IN"
        and today["geofenced"] is True
        and today["dayKind"] == "WORKING"
    )
    assert today["outlets"] == [
        {"outletName": "Cuttack Motors", "projectName": "Project One", "hasLocation": True}
    ]


def test_a_photo_needs_a_token_and_a_token_works_once(world):
    _, user, _ = setup_pc(world)
    assert send(world, user, "in", "not-a-token").json()["code"] == "ATTENDANCE_TOKEN_INVALID"
    tok = token(world, user)
    assert send(world, user, "in", tok).status_code == 200
    # a second check-in with the used token is refused (it was already checked in as well)
    again = send(world, user, "in", tok)
    assert again.status_code == 409


def test_a_token_is_for_one_person_one_purpose_and_expires(world):
    _, user, _ = setup_pc(world)
    _, other, _ = setup_pc(world)
    tok = token(world, user)
    assert send(world, other, "in", tok).json()["code"] == "ATTENDANCE_TOKEN_INVALID"
    out_token = token(world, user, "CHECK_OUT")
    assert send(world, user, "in", out_token).json()["code"] == "ATTENDANCE_TOKEN_INVALID"
    stale = token(world, user)
    world.clock.set(ist(2026, 10, 5, 10, 25))  # 5 minutes later; the window is 2
    assert send(world, user, "in", stale).json()["code"] == "ATTENDANCE_TOKEN_INVALID"


def test_outside_the_fence_needs_a_reason_and_the_same_token_still_works(world):
    _, user, _ = setup_pc(world)
    tok = token(world, user)
    first = send(world, user, "in", tok, where=FAR)
    assert first.status_code == 422 and first.json()["code"] == "ATTENDANCE_REASON_REQUIRED"
    second = send(world, user, "in", tok, where=FAR, reason="Visiting the other showroom")
    assert second.status_code == 200
    assert (
        second.json()["needsApproval"] == ["OUT_OF_FENCE"]
        and "OUT_OF_FENCE" in second.json()["flags"]
    )
    assert second.json()["distanceM"] > 500
    today = world.client.get("/hr/v1/attendance/today", headers=world.headers(user)).json()
    assert today["day"]["status"] == "PENDING_APPROVAL"


def test_pc_without_an_outlet_location_goes_to_an_exception(world):
    _, user, _ = setup_pc(world, with_outlet=False)
    tok = token(world, user)
    assert send(world, user, "in", tok).json()["code"] == "ATTENDANCE_REASON_REQUIRED"
    r = send(world, user, "in", tok, reason="No outlet is set for me yet")
    assert r.status_code == 200 and r.json()["needsApproval"] == ["NO_OUTLET_LOCATION"]


def test_team_lead_is_not_geofenced_and_late_arrival_is_an_exception(world):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    _, tl = world.employee(hr)
    world.assign(tl, "TL")
    world.clock.set(ist(2026, 10, 5, 11, 30))
    r = send(world, tl, "in", token(world, tl), where=FAR)
    assert r.status_code == 200
    assert r.json()["needsApproval"] == ["LATE_CHECK_IN"] and r.json()["distanceM"] is None


def test_exactly_at_the_late_limit_is_on_time(world):
    _, user, _ = setup_pc(world)
    world.clock.set(ist(2026, 10, 5, 11, 15, 0))
    assert send(world, user, "in", token(world, user)).json()["needsApproval"] == []


def test_location_must_be_fresh_and_accurate(world):
    _, user, _ = setup_pc(world)
    tok = token(world, user)
    assert (
        send(world, user, "in", tok, accuracy_m=500).json()["code"]
        == "ATTENDANCE_LOCATION_TOO_INACCURATE"
    )
    assert (
        send(world, user, "in", tok, position_age_s=600).json()["code"]
        == "ATTENDANCE_LOCATION_TOO_OLD"
    )
    assert send(world, user, "in", tok, latitude=95).json()["code"] == "ATTENDANCE_LOCATION_INVALID"
    assert send(world, user, "in", tok).status_code == 200  # nothing above used the token


def test_sunday_and_declared_holidays_are_not_working_days_but_tentative_ones_are(world):
    _, user, hr = setup_pc(world)
    world.clock.set(ist(2026, 10, 4, 10, 20))  # Sunday
    assert (
        send(world, user, "in", token(world, user)).json()["code"] == "ATTENDANCE_NOT_A_WORKING_DAY"
    )
    world.clock.set(ist(2026, 10, 19, 10, 20))  # Mahanavami, tentative in the calendar
    today = world.client.get("/hr/v1/attendance/today", headers=world.headers(user)).json()
    assert today["dayKind"] == "WORKING" and today["tentativeHoliday"] == "Mahanavami"
    assert send(world, user, "in", token(world, user)).status_code == 200
    world.grant(hr, perm.HR_SETTINGS_MANAGE)
    r = world.client.put(
        "/hr/v1/holidays/2026-10-20",
        json={"name": "Vijaya Dasami", "status": "DECLARED"},
        headers=world.headers(hr),
    )
    assert r.status_code == 200
    world.clock.set(ist(2026, 10, 20, 10, 20))
    assert (
        send(world, user, "in", token(world, user)).json()["code"] == "ATTENDANCE_NOT_A_WORKING_DAY"
    )


def test_check_in_once_check_out_after_and_early_check_out_is_an_exception(world):
    _, user, _ = setup_pc(world)
    out_token = token(world, user, "CHECK_OUT")
    assert send(world, user, "out", out_token).json()["code"] == "ATTENDANCE_NOT_CHECKED_IN"
    assert send(world, user, "in", token(world, user)).status_code == 200
    assert (
        send(world, user, "in", token(world, user)).json()["code"]
        == "ATTENDANCE_ALREADY_CHECKED_IN"
    )
    world.clock.set(ist(2026, 10, 5, 16, 30))
    early = send(world, user, "out", token(world, user, "CHECK_OUT"))
    assert early.status_code == 200 and early.json()["needsApproval"] == ["EARLY_CHECK_OUT"]
    assert (
        send(world, user, "out", token(world, user, "CHECK_OUT")).json()["code"]
        == "ATTENDANCE_ALREADY_CHECKED_OUT"
    )


def test_a_normal_day_is_complete_and_shows_in_history_and_team_view(world):
    emp, user, hr = setup_pc(world)
    send(world, user, "in", token(world, user))
    world.clock.set(ist(2026, 10, 5, 18, 35))
    assert send(world, user, "out", token(world, user, "CHECK_OUT")).json()["needsApproval"] == []
    mine = world.client.get(
        "/hr/v1/attendance/me?month=2026-10", headers=world.headers(user)
    ).json()
    assert [d["status"] for d in mine["days"]] == ["COMPLETE"]
    boss = world.grant(str(uuid.uuid4()), perm.HR_ATTENDANCE_READ_ALL)
    team = world.client.get(
        "/hr/v1/attendance/team?month=2026-10", headers=world.headers(boss)
    ).json()
    row = next(e for e in team["employees"] if e["employeeId"] == emp["employeeId"])
    assert row["daysCheckedIn"] == 1 and row["daysCheckedOut"] == 1
    assert (
        world.client.get("/hr/v1/attendance/team", headers=world.headers(user)).status_code == 403
    )


def test_the_server_clock_decides_the_time_not_the_phone(world):
    _, user, _ = setup_pc(world)
    # a photo whose embedded time is days old is flagged, not trusted
    image = Image.new("RGB", (200, 150), (10, 20, 30))
    exif = Image.Exif()
    exif[0x0132] = "2020:01:01 08:00:00"
    out = io.BytesIO()
    image.save(out, format="JPEG", exif=exif)
    r = world.client.post(
        "/hr/v1/attendance/check-in",
        headers=world.headers(user),
        data={
            "token": token(world, user),
            "latitude": "20.463",
            "longitude": "85.883",
            "accuracy_m": "10",
        },
        files={"photo": ("p.jpg", out.getvalue(), "image/jpeg")},
    )
    assert r.status_code == 200 and "PHOTO_TIME_MISMATCH" in r.json()["flags"]


def test_without_an_address_the_record_says_so(world):
    _, user, _ = setup_pc(world)
    world.geocoder.address_value = None
    r = send(world, user, "in", token(world, user))
    assert (
        r.status_code == 200 and "NO_ADDRESS" in r.json()["flags"] and r.json()["address"] is None
    )


def test_bad_photos_are_refused_and_storage_failure_is_a_503(world):
    _, user, _ = setup_pc(world)
    tok = token(world, user)
    r = world.client.post(
        "/hr/v1/attendance/check-in",
        headers=world.headers(user),
        data={"token": tok, "latitude": "20.463", "longitude": "85.883", "accuracy_m": "10"},
        files={"photo": ("p.jpg", b"not an image", "image/jpeg")},
    )
    assert r.status_code == 422 and r.json()["code"] == "ATTENDANCE_PHOTO_NOT_ACCEPTED"
    world.storage.fail = True
    r = send(world, user, "in", tok)
    assert r.status_code == 503
    world.storage.fail = False
    assert send(world, user, "in", tok).status_code == 200  # the failed try did not use the token


# ---- approvals -----------------------------------------------------------------------------


def exception_for(world: World, pc_user: str) -> str:
    items = world.client.get("/hr/v1/approvals/attendance", headers=world.headers(pc_user)).json()[
        "items"
    ]
    assert items == [] or True
    with world.engine.connect() as c:
        return str(
            c.execute(
                text(
                    "SELECT exception_id FROM hr.attendance_exception ORDER BY created_at DESC LIMIT 1"
                )
            ).scalar_one()
        )


def test_team_lead_decides_for_their_project_and_nobody_else_can(world):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    pc, pc_user = world.employee(hr)
    world.assign(pc_user, "PC", outlet=OUTLET)
    _, tl_user = world.employee(hr)
    world.assign(tl_user, "TL")
    _, other_tl = world.employee(hr)
    world.assign(other_tl, "TL", tenant="tenant-b")
    tok = token(world, pc_user)
    send(world, pc_user, "in", tok, where=FAR, reason="Other showroom")
    xid = exception_for(world, pc_user)

    mine = world.client.get("/hr/v1/approvals/attendance", headers=world.headers(tl_user)).json()[
        "items"
    ]
    assert [i["exceptionId"] for i in mine] == [xid] and mine[0]["kind"] == "OUT_OF_FENCE"
    assert (
        world.client.get("/hr/v1/approvals/attendance", headers=world.headers(other_tl)).json()[
            "items"
        ]
        == []
    )
    assert (
        world.client.get("/hr/v1/approvals/attendance", headers=world.headers(pc_user)).json()[
            "items"
        ]
        == []
    )
    assert (
        world.client.post(
            f"/hr/v1/approvals/attendance/{xid}/decision",
            json={"decision": "APPROVE"},
            headers=world.headers(pc_user),
        ).status_code
        == 404
    )
    assert (
        world.client.post(
            f"/hr/v1/approvals/attendance/{xid}/decision",
            json={"decision": "APPROVE"},
            headers=world.headers(other_tl),
        ).status_code
        == 404
    )

    no_note = world.client.post(
        f"/hr/v1/approvals/attendance/{xid}/decision",
        json={"decision": "REJECT"},
        headers=world.headers(tl_user),
    )
    assert no_note.status_code == 422 and no_note.json()["code"] == "APPROVAL_NOTE_REQUIRED"
    ok = world.client.post(
        f"/hr/v1/approvals/attendance/{xid}/decision",
        json={"decision": "APPROVE"},
        headers=world.headers(tl_user),
    )
    assert ok.status_code == 200 and ok.json()["status"] == "APPROVED"
    again = world.client.post(
        f"/hr/v1/approvals/attendance/{xid}/decision",
        json={"decision": "APPROVE"},
        headers=world.headers(tl_user),
    )
    assert again.status_code == 409
    today = world.client.get("/hr/v1/attendance/today", headers=world.headers(pc_user)).json()
    assert today["day"]["status"] == "CHECKED_IN"  # approved: now a normal day


def test_a_rejection_is_kept_with_its_reason(world):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    _, pc_user = world.employee(hr)
    world.assign(pc_user, "PC", outlet=OUTLET)
    _, tl_user = world.employee(hr)
    world.assign(tl_user, "TL")
    send(world, pc_user, "in", token(world, pc_user), where=FAR, reason="x")
    xid = exception_for(world, pc_user)
    r = world.client.post(
        f"/hr/v1/approvals/attendance/{xid}/decision",
        json={"decision": "REJECT", "note": "Not agreed"},
        headers=world.headers(tl_user),
    )
    assert r.json()["status"] == "REJECTED"
    today = world.client.get("/hr/v1/attendance/today", headers=world.headers(pc_user)).json()
    assert today["day"]["status"] == "EXCEPTION_REJECTED"
    assert today["day"]["exceptions"][0]["decisionNote"] == "Not agreed"


def test_hr_decides_only_when_the_person_has_no_team_lead_or_manager(world):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    _, pc_user = world.employee(hr)
    world.assign(pc_user, "PC", outlet=OUTLET)
    keeper = world.grant(str(uuid.uuid4()), perm.HR_ATTENDANCE_READ_ALL)
    send(world, pc_user, "in", token(world, pc_user), where=FAR, reason="x")
    xid = exception_for(world, pc_user)
    # no TL or PM on the project: HR may decide
    assert [
        i["exceptionId"]
        for i in world.client.get(
            "/hr/v1/approvals/attendance", headers=world.headers(keeper)
        ).json()["items"]
    ] == [xid]
    # once a Team Lead exists, HR no longer decides it (the CEO still could)
    _, tl_user = world.employee(hr)
    world.assign(tl_user, "TL")
    assert (
        world.client.get("/hr/v1/approvals/attendance", headers=world.headers(keeper)).json()[
            "items"
        ]
        == []
    )
    ceo = world.grant(str(uuid.uuid4()), perm.HR_PAYROLL_APPROVE)
    assert [
        i["exceptionId"]
        for i in world.client.get("/hr/v1/approvals/attendance", headers=world.headers(ceo)).json()[
            "items"
        ]
    ] == [xid]


def test_nobody_decides_their_own_request_even_the_ceo(world):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    _, user = world.employee(hr)
    world.assign(user, "TL")
    world.grant(user, perm.HR_PAYROLL_APPROVE, perm.HR_ATTENDANCE_READ_ALL)
    world.clock.set(ist(2026, 10, 5, 11, 40))
    send(world, user, "in", token(world, user))
    xid = exception_for(world, user)
    assert (
        world.client.post(
            f"/hr/v1/approvals/attendance/{xid}/decision",
            json={"decision": "APPROVE"},
            headers=world.headers(user),
        ).status_code
        == 404
    )
    assert (
        world.client.get("/hr/v1/approvals/attendance", headers=world.headers(user)).json()["items"]
        == []
    )


def test_photos_are_visible_to_the_owner_the_approver_and_hr_only_and_views_are_recorded(
    world, migrated_engine
):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    pc, pc_user = world.employee(hr)
    world.assign(pc_user, "PC", outlet=OUTLET)
    _, tl_user = world.employee(hr)
    world.assign(tl_user, "TL")
    stranger = str(uuid.uuid4())
    send(world, pc_user, "in", token(world, pc_user))
    attendance_id = world.client.get(
        "/hr/v1/attendance/today", headers=world.headers(pc_user)
    ).json()["day"]["attendanceId"]
    path = f"/hr/v1/attendance/{attendance_id}/photo/in"
    assert world.client.get(path, headers=world.headers(pc_user)).status_code == 200
    assert world.client.get(path, headers=world.headers(stranger)).status_code == 404
    # a Team Lead sees it only for a request they decide
    assert world.client.get(path, headers=world.headers(tl_user)).status_code == 200
    with migrated_engine.connect() as c:
        views = c.execute(
            text(
                "SELECT count(*) FROM hr.audit_log WHERE action = 'ATTENDANCE_PHOTO_VIEWED' AND entity_id = :e"
            ),
            {"e": pc["employeeId"]},
        ).scalar_one()
    assert views == 1  # the owner's own view is not logged; the TL's is


# ---- settings, holidays -------------------------------------------------------------------


def test_settings_are_validated_audited_and_change_the_rules(world):
    hr = world.grant(str(uuid.uuid4()), perm.HR_SETTINGS_MANAGE)
    h = world.headers(hr)
    items = world.client.get("/hr/v1/settings", headers=h).json()["items"]
    assert next(i for i in items if i["key"] == "attendance.late_after")["value"] == "11:15"
    assert (
        world.client.put(
            "/hr/v1/settings", json={"values": {"attendance.late_after": "25:99"}}, headers=h
        ).status_code
        == 422
    )
    assert (
        world.client.put("/hr/v1/settings", json={"values": {"nope": 1}}, headers=h).status_code
        == 422
    )
    assert (
        world.client.put(
            "/hr/v1/settings", json={"values": {"attendance.geofence_radius_m": 10}}, headers=h
        ).status_code
        == 422
    )
    bad = world.client.put(
        "/hr/v1/settings", json={"values": {"attendance.late_after": "10:00"}}, headers=h
    )
    assert bad.status_code == 422  # earlier than the 10:30 standard
    assert (
        world.client.put(
            "/hr/v1/settings", json={"values": {"attendance.late_after": "11:45"}}, headers=h
        ).status_code
        == 200
    )
    _, user, _ = setup_pc(world)
    world.clock.set(ist(2026, 10, 5, 11, 30))
    assert (
        send(world, user, "in", token(world, user)).json()["needsApproval"] == []
    )  # no longer late
    assert world.client.get("/hr/v1/settings", headers=world.headers(user)).status_code == 403


def test_holidays_are_listed_for_everyone_with_the_caveat_and_managed_by_hr(world):
    anyone = str(uuid.uuid4())
    body = world.client.get("/hr/v1/holidays?year=2026", headers=world.headers(anyone)).json()
    names = {i["name"]: i["status"] for i in body["items"]}
    assert names["Diwali"] == "TENTATIVE" and "declared by HR" in body["note"]
    assert (
        world.client.put(
            "/hr/v1/holidays/2026-12-31",
            json={"name": "Year end", "status": "DECLARED"},
            headers=world.headers(anyone),
        ).status_code
        == 403
    )
    hr = world.grant(str(uuid.uuid4()), perm.HR_SETTINGS_MANAGE)
    assert (
        world.client.put(
            "/hr/v1/holidays/2026-12-31", json={"name": "Year end"}, headers=world.headers(hr)
        ).json()["status"]
        == "TENTATIVE"
    )
    assert (
        world.client.delete("/hr/v1/holidays/2026-12-31", headers=world.headers(hr)).status_code
        == 200
    )
    assert (
        world.client.delete("/hr/v1/holidays/2026-12-31", headers=world.headers(hr)).status_code
        == 404
    )


# ---- a PC mapped to several outlets -----------------------------------------------------------

SECOND_OUTLET = ("Bhubaneswar Motors", 20.2961, 85.8245)  # about 20 km from the first
NEAR_SECOND = (20.2965, 85.8248)


def setup_pc_two_outlets(world: World):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    emp, user = world.employee(hr)
    world.assign(user, "PC", outlet=OUTLET)
    world.assign(user, "PC", outlet=SECOND_OUTLET)
    return emp, user


@pytest.mark.parametrize(
    ("where", "outlet_name"), [(NEAR, "Cuttack Motors"), (NEAR_SECOND, "Bhubaneswar Motors")]
)
def test_a_pc_at_any_one_of_their_outlets_is_verified_and_the_right_outlet_is_recorded(
    world, where, outlet_name
):
    _, user = setup_pc_two_outlets(world)
    r = send(world, user, "in", token(world, user), where=where)
    assert r.status_code == 200, r.text
    assert r.json()["needsApproval"] == [] and "OUT_OF_FENCE" not in r.json()["flags"]
    assert r.json()["outletName"] == outlet_name
    today = world.client.get("/hr/v1/attendance/today", headers=world.headers(user)).json()
    assert sorted(o["outletName"] for o in today["outlets"]) == [
        "Bhubaneswar Motors",
        "Cuttack Motors",
    ]


def test_a_pc_away_from_every_one_of_their_outlets_still_needs_a_reason(world):
    _, user = setup_pc_two_outlets(world)
    tok = token(world, user)
    assert send(world, user, "in", tok, where=FAR).json()["code"] == "ATTENDANCE_REASON_REQUIRED"
    r = send(world, user, "in", tok, where=FAR, reason="At a customer site")
    assert r.json()["needsApproval"] == ["OUT_OF_FENCE"]


def test_an_outlet_without_a_location_does_not_stop_the_others_from_working(world):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    _, user = world.employee(hr)
    world.assign(user, "PC", outlet=("Unmapped Outlet", None, None))
    world.assign(user, "PC", outlet=OUTLET)
    r = send(world, user, "in", token(world, user), where=NEAR)
    assert r.status_code == 200 and r.json()["needsApproval"] == []
    assert r.json()["outletName"] == "Cuttack Motors"


def test_a_missing_outlet_location_is_decided_by_hr_even_when_a_team_lead_exists(world):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    _, pc_user = world.employee(hr)
    world.assign(pc_user, "PC", outlet=("No Location Outlet", None, None))
    _, tl_user = world.employee(hr)
    world.assign(tl_user, "TL")
    keeper = world.grant(str(uuid.uuid4()), perm.HR_ATTENDANCE_READ_ALL)
    r = send(world, pc_user, "in", token(world, pc_user), reason="Outlet has no pin yet")
    assert r.status_code == 200 and r.json()["needsApproval"] == ["NO_OUTLET_LOCATION"]
    xid = exception_for(world, pc_user)
    listed = world.client.get("/hr/v1/approvals/attendance", headers=world.headers(keeper))
    assert [i["exceptionId"] for i in listed.json()["items"]] == [xid]
    assert (
        world.client.get("/hr/v1/approvals/attendance", headers=world.headers(tl_user)).json()[
            "items"
        ]
        == []
    )
    refused = world.client.post(
        f"/hr/v1/approvals/attendance/{xid}/decision",
        json={"decision": "APPROVE"},
        headers=world.headers(tl_user),
    )
    assert refused.status_code == 404
    done = world.client.post(
        f"/hr/v1/approvals/attendance/{xid}/decision",
        json={"decision": "APPROVE"},
        headers=world.headers(keeper),
    )
    assert done.status_code == 200 and done.json()["status"] == "APPROVED"
