"""Full and half-day holidays, for the whole company or chosen projects and outlets
(product owner, 7 Nov 2026)."""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from hrmgmt import holidays as hol
from hrmgmt import leave_rules as lr
from hrmgmt import payroll_data as pdata
from hrmgmt import permissions as perm
from hrmgmt import settings_store as cfg
from tests.support import OUTLET, World, ist
from tests.test_attendance import send, token

TUE = date(2026, 11, 10)
WED = date(2026, 11, 11)
THU = date(2026, 11, 12)
O1 = str(uuid.UUID(int=101))
O2 = str(uuid.UUID(int=102))
O3 = str(uuid.UUID(int=103))


@pytest.fixture()
def world(migrated_engine):
    w = World(migrated_engine, now=ist(2026, 11, 9, 10, 0))  # Monday 9 Nov 2026
    w.clean_assignments()
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE hr.leave_ledger, hr.leave_request, hr.leave_accrual,"
                " hr.attendance_exception, hr.attendance_day, hr.capture_token CASCADE"
            )
        )
        conn.execute(text("DELETE FROM hr.setting"))
        conn.execute(
            text("DELETE FROM hr.holiday WHERE status = 'DECLARED' OR removed_at IS NOT NULL")
        )
        conn.execute(text("DELETE FROM hr.holiday WHERE created_by IS NOT NULL"))
    return w


def setting(world: World, key: str, value) -> None:
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hr.setting (key, value) VALUES (:k, CAST(:v AS jsonb))"
                " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
            ),
            {"k": key, "v": json.dumps(value)},
        )


def manager(world: World) -> str:
    return world.grant(str(uuid.uuid4()), perm.HR_SETTINGS_MANAGE)


def add(world: World, hr: str, day: date, name: str, **body):
    payload = {"date": day.isoformat(), "name": name, "status": "DECLARED", **body}
    return world.client.post("/hr/v1/holidays", json=payload, headers=world.headers(hr))


def staff(world: World):
    """PC at outlet O1 of project A; PC at outlet O2 of project B; a TL on A and B (shared); a head
    office person with no assignment. Returns {name: (employee_id, user_id)} and the HR user."""
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    people = {}
    for key in ("pc_a", "pc_b", "shared", "office"):
        emp, user = world.employee(hr)
        people[key] = (emp["employeeId"], user)
    world.assign(
        people["pc_a"][1], "PC", tenant="t-a", project=("A", "Alpha"), outlet=OUTLET, outlet_id=O1
    )
    world.assign(
        people["pc_b"][1], "PC", tenant="t-b", project=("B", "Bravo"), outlet=OUTLET, outlet_id=O2
    )
    world.assign(people["shared"][1], "TL", tenant="t-a", project=("A", "Alpha"))
    world.assign(people["shared"][1], "TL", tenant="t-b", project=("B", "Bravo"))
    world.assign(people["pc_b"][1], "TL", tenant="t-b", project=("B", "Bravo"))  # same project
    with world.engine.begin() as conn:  # an outlet of B nobody above works at
        conn.execute(
            text(
                "INSERT INTO hr.work_assignment (security_user_id, tenant_id, project_code,"
                " project_name, role_code, outlet_id, outlet_name, valid_from, last_seen_at)"
                " VALUES (gen_random_uuid(), 't-b', 'B', 'Bravo', 'PC', CAST(:o AS uuid),"
                " 'Puri Motors', '2026-01-01', now())"
            ),
            {"o": O3},
        )
    return people, hr


# ---- rules on their own ------------------------------------------------------------------------


def test_day_parts_combine_full_day_wins_and_two_halves_make_a_full_day():
    assert hol.combine([]) is None
    assert hol.combine([("MORNING", "A")]) == hol.DayHoliday("MORNING", ("A",))
    assert hol.combine([("MORNING", "A"), ("FULL_DAY", "B")]) == hol.DayHoliday("FULL_DAY", ("B",))
    both = hol.combine([("MORNING", "A"), ("AFTERNOON", "B")])
    assert both is not None and both.is_full and both.name == "A / B"
    assert hol.combine([("AFTERNOON", "A"), ("AFTERNOON", "A")]) == hol.DayHoliday(
        "AFTERNOON", ("A",)
    )


def test_times_of_the_working_half_follow_the_split():
    values = {key: spec.default for key, spec in cfg.SPECS.items()}
    assert values["attendance.half_day_split"] == "14:00"
    assert hol.effective_values(values, None) is values
    morning = hol.effective_values(values, "MORNING")
    assert morning["attendance.check_in_standard"] == "14:00"
    assert morning["attendance.late_after"] == "14:45"  # the usual 45 minutes, from the split
    assert morning["attendance.check_out_earliest"] == "17:00"
    assert morning["attendance.absent_after"] == "17:00"
    assert morning["attendance.min_work_hours"] == Decimal("2.5")
    afternoon = hol.effective_values(values, "AFTERNOON")
    assert afternoon["attendance.late_after"] == "11:15"
    assert afternoon["attendance.check_out_earliest"] == "14:00"
    assert afternoon["attendance.absent_after"] == "14:00"
    assert afternoon["attendance.min_work_hours"] == Decimal("2.5")
    # working the off half: at least half of the minimum hours inside it
    assert hol.off_half_worked(timedelta(hours=2, minutes=30), values)
    assert not hol.off_half_worked(timedelta(hours=2, minutes=29), values)


# ---- who a holiday applies to ------------------------------------------------------------------


def test_scope_matching_all_project_outlet_no_assignment_and_shared(world):
    people, _ = staff(world)
    hr = manager(world)
    assert add(world, hr, TUE, "Company day").status_code == 201
    r = add(world, hr, WED, "Alpha day", appliesTo="SELECTED", scope=[{"tenantId": "t-a"}])
    assert r.status_code == 201, r.text
    r = add(
        world,
        hr,
        THU,
        "Bravo outlet half",
        dayPart="AFTERNOON",
        appliesTo="SELECTED",
        scope=[{"tenantId": "t-b", "outletId": O2}],
    )
    assert r.status_code == 201, r.text
    assert r.json()["scope"] == [
        {
            "tenantId": "t-b",
            "projectCode": "B",
            "projectName": "Bravo",
            "outletId": O2,
            "outletCode": None,
            "outletName": "Cuttack Motors",
        }
    ]
    ids = [people[k][0] for k in ("pc_a", "pc_b", "shared", "office")]
    with world.engine.connect() as conn:
        got = hol.holidays_by_employee(conn, ids, TUE, THU)
    pc_a, pc_b, shared, office = (got[i] for i in ids)
    # company-wide: everyone, including head office with no assignment
    assert all(TUE in m and m[TUE].is_full for m in (pc_a, pc_b, shared, office))
    # project Alpha: its PC and the TL shared with Bravo; not Bravo's PC, not head office
    assert WED in pc_a and WED in shared and WED not in pc_b and WED not in office
    # one outlet of Bravo: only the PC at that outlet (the TL on Bravo has no outlet there)
    assert pc_b[THU] == hol.DayHoliday("AFTERNOON", ("Bravo outlet half",))
    assert THU not in pc_a and THU not in shared and THU not in office


def test_an_assignment_must_be_valid_on_the_holiday_date(world):
    people, _ = staff(world)
    hr = manager(world)
    assert (
        add(
            world, hr, WED, "Alpha day", appliesTo="SELECTED", scope=[{"tenantId": "t-a"}]
        ).status_code
        == 201
    )
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.work_assignment SET valid_to = '2026-11-10T00:00:00+05:30'"
                " WHERE security_user_id = CAST(:u AS uuid)"
            ),
            {"u": people["pc_a"][1]},
        )
    with world.engine.connect() as conn:
        assert hol.holiday_for(conn, people["pc_a"][0], WED) is None
        assert hol.holiday_for(conn, people["shared"][0], WED) is not None


def test_a_morning_and_an_afternoon_for_the_same_person_make_a_full_holiday(world):
    people, _ = staff(world)
    hr = manager(world)
    assert add(world, hr, TUE, "Eve", dayPart="MORNING").status_code == 201
    assert (
        add(
            world,
            hr,
            TUE,
            "Bravo puja",
            dayPart="AFTERNOON",
            appliesTo="SELECTED",
            scope=[{"tenantId": "t-b"}],
        ).status_code
        == 201
    )
    with world.engine.connect() as conn:
        assert hol.holiday_for(conn, people["pc_b"][0], TUE) == hol.DayHoliday(
            "FULL_DAY", ("Eve", "Bravo puja")
        )
        assert hol.holiday_for(conn, people["pc_a"][0], TUE) == hol.DayHoliday("MORNING", ("Eve",))


def test_a_tentative_or_removed_holiday_stops_nothing(world):
    people, _ = staff(world)
    hr = manager(world)
    tentative = add(world, hr, TUE, "Maybe", status="TENTATIVE").json()
    gone = add(world, hr, WED, "Gone").json()
    assert (
        world.client.delete(
            f"/hr/v1/holidays/entries/{gone['holidayId']}", headers=world.headers(hr)
        ).status_code
        == 200
    )
    with world.engine.connect() as conn:
        assert hol.holidays_for(conn, people["pc_a"][0], TUE, WED) == {}
        assert hol.holiday_for(conn, people["pc_a"][0], TUE, status="TENTATIVE") is not None
    assert tentative["status"] == "TENTATIVE"


# ---- the admin routes ------------------------------------------------------------------------


def test_only_hr_or_superadmin_manage_holidays_and_every_change_is_audited(world):
    staff(world)
    someone = str(uuid.uuid4())
    h = world.headers(someone)
    assert add(world, someone, TUE, "Nope").status_code == 403
    assert world.client.get("/hr/v1/holidays/scope-options", headers=h).status_code == 403
    hr = manager(world)
    created = add(world, hr, TUE, "Diwali eve", dayPart="AFTERNOON", status="TENTATIVE")
    assert created.status_code == 201
    hid = created.json()["holidayId"]
    path = f"/hr/v1/holidays/entries/{hid}"
    body = {
        "date": TUE.isoformat(),
        "name": "Diwali eve",
        "status": "DECLARED",
        "dayPart": "AFTERNOON",
        "appliesTo": "SELECTED",
        "scope": [{"tenantId": "t-a"}, {"tenantId": "t-a", "outletId": O1}],
    }
    assert world.client.put(path, json=body, headers=h).status_code == 403
    assert world.client.delete(path, headers=h).status_code == 403
    changed = world.client.put(path, json=body, headers=world.headers(hr))
    assert changed.status_code == 200, changed.text
    # the outlet is inside the project chosen whole, so only the project is kept
    assert [s["outletId"] for s in changed.json()["scope"]] == [None]
    assert changed.json()["status"] == "DECLARED"
    assert world.client.delete(path, headers=world.headers(hr)).status_code == 200
    assert world.client.delete(path, headers=world.headers(hr)).status_code == 404
    with world.engine.connect() as conn:
        audit = conn.execute(
            text(
                "SELECT action, changes, actor_user_id FROM hr.audit_log"
                " WHERE entity_type = 'holiday' AND entity_id = :h ORDER BY audit_id"
            ),
            {"h": hid},
        ).all()
        kept = conn.execute(
            text(
                "SELECT removed_at IS NOT NULL, removed_by FROM hr.holiday"
                " WHERE holiday_id = CAST(:h AS uuid)"
            ),
            {"h": hid},
        ).one()
    assert [a[0] for a in audit] == ["HOLIDAY_ADDED", "HOLIDAY_CHANGED", "HOLIDAY_REMOVED"]
    assert all(a[2] == hr for a in audit)
    assert audit[0][1]["before"] is None and audit[0][1]["after"]["status"] == "TENTATIVE"
    assert audit[1][1]["before"]["appliesTo"] == "ALL"
    assert audit[1][1]["after"]["appliesTo"] == "SELECTED"
    assert audit[1][1]["after"]["scope"][0]["projectName"] == "Alpha"
    assert audit[2][1]["before"]["status"] == "DECLARED" and audit[2][1]["after"] is None
    assert kept == (True, hr)  # removed, not deleted


def test_holiday_input_is_checked(world):
    staff(world)
    hr = manager(world)
    assert add(world, hr, TUE, "x").status_code == 422  # name too short
    no_scope = add(world, hr, TUE, "Picked", appliesTo="SELECTED")
    assert no_scope.status_code == 422 and no_scope.json()["code"] == "HOLIDAY_INVALID"
    stray = add(world, hr, TUE, "All", scope=[{"tenantId": "t-a"}])
    assert stray.status_code == 422
    unknown = add(world, hr, TUE, "Nowhere", appliesTo="SELECTED", scope=[{"tenantId": "t-zz"}])
    assert unknown.status_code == 422 and "work context" in unknown.json()["detail"]
    assert add(world, hr, TUE, "Bad part", dayPart="EVENING").status_code == 422


def test_overlapping_holidays_are_refused_with_the_name_of_the_one_in_the_way(world):
    staff(world)
    hr = manager(world)
    assert add(world, hr, TUE, "Eve", dayPart="MORNING").status_code == 201
    clash = add(world, hr, TUE, "Whole day")
    assert clash.status_code == 409 and clash.json()["code"] == "HOLIDAY_CLASH"
    assert "Eve (morning off)" in clash.json()["detail"]
    # the other half is free, and a project on another date too
    assert add(world, hr, TUE, "Afternoon", dayPart="AFTERNOON").status_code == 201
    assert (
        add(world, hr, WED, "Alpha", appliesTo="SELECTED", scope=[{"tenantId": "t-a"}]).status_code
        == 201
    )
    outlet_of_alpha = add(
        world, hr, WED, "O1", appliesTo="SELECTED", scope=[{"tenantId": "t-a", "outletId": O1}]
    )
    assert outlet_of_alpha.status_code == 409
    other_project = add(world, hr, WED, "Bravo", appliesTo="SELECTED", scope=[{"tenantId": "t-b"}])
    assert other_project.status_code == 201


def test_the_list_shows_hr_everything_and_everyone_else_their_own(world):
    people, _ = staff(world)
    hr = manager(world)
    add(world, hr, TUE, "Company day", dayPart="MORNING")
    add(world, hr, WED, "Alpha day", appliesTo="SELECTED", scope=[{"tenantId": "t-a"}])
    everything = world.client.get("/hr/v1/holidays?year=2026", headers=world.headers(hr)).json()
    by_name = {i["name"]: i for i in everything["items"]}
    assert {"Company day", "Alpha day", "Diwali"} <= set(by_name)  # Diwali: tentative, from 0004
    assert by_name["Alpha day"]["scope"][0]["projectName"] == "Alpha"
    item = by_name["Company day"]
    assert {"date", "name", "status", "holidayId", "dayPart", "appliesTo", "scope"} <= set(item)
    assert item["dayPart"] == "MORNING" and item["appliesTo"] == "ALL"

    def names(user):
        body = world.client.get("/hr/v1/holidays?year=2026", headers=world.headers(user)).json()
        return {i["name"] for i in body["items"]}

    assert "Alpha day" in names(people["pc_a"][1]) and "Alpha day" in names(people["shared"][1])
    assert "Alpha day" not in names(people["pc_b"][1])
    assert "Alpha day" not in names(people["office"][1])
    assert "Company day" in names(people["office"][1])
    mine = world.client.get("/hr/v1/holidays?year=2026&mine=true", headers=world.headers(hr)).json()
    assert "Alpha day" not in {i["name"] for i in mine["items"]}  # HR is not on Alpha


def test_scope_options_list_projects_with_their_outlets(world):
    staff(world)
    hr = manager(world)
    body = world.client.get("/hr/v1/holidays/scope-options", headers=world.headers(hr)).json()
    projects = {p["tenantId"]: p for p in body["projects"]}
    assert projects["t-a"]["projectName"] == "Alpha"
    assert [o["outletId"] for o in projects["t-a"]["outlets"]] == [O1]
    assert {o["outletName"] for o in projects["t-b"]["outlets"]} == {
        "Cuttack Motors",
        "Puri Motors",
    }


def test_the_earlier_routes_by_date_still_set_and_remove_a_company_full_day(world):
    hr = manager(world)
    h = world.headers(hr)
    put = world.client.put("/hr/v1/holidays/2026-11-10", json={"name": "Old way"}, headers=h)
    assert put.status_code == 200 and put.json()["status"] == "TENTATIVE"
    again = world.client.put(
        "/hr/v1/holidays/2026-11-10", json={"name": "Old way", "status": "DECLARED"}, headers=h
    )
    assert again.json()["holidayId"] == put.json()["holidayId"]  # the same holiday, changed
    items = world.client.get("/hr/v1/holidays?year=2026", headers=h).json()["items"]
    assert [i["dayPart"] for i in items if i["name"] == "Old way"] == ["FULL_DAY"]
    assert world.client.delete("/hr/v1/holidays/2026-11-10", headers=h).status_code == 200
    assert world.client.delete("/hr/v1/holidays/2026-11-10", headers=h).status_code == 404


def test_the_split_is_a_setting_hr_can_change_within_the_day(world):
    hr = manager(world)
    h = world.headers(hr)
    items = world.client.get("/hr/v1/settings", headers=h).json()["items"]
    split = next(i for i in items if i["key"] == "attendance.half_day_split")
    assert split["value"] == "14:00" and "afternoon starts" in split["label"]
    for bad in ("11:00", "17:30"):
        r = world.client.put(
            "/hr/v1/settings", json={"values": {"attendance.half_day_split": bad}}, headers=h
        )
        assert r.status_code == 422
    ok = world.client.put(
        "/hr/v1/settings", json={"values": {"attendance.half_day_split": "13:30"}}, headers=h
    )
    assert ok.status_code == 200


# ---- attendance on a half-day holiday ----------------------------------------------------------


def pc_with_lead(world: World):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    emp, pc = world.employee(hr)
    world.assign(pc, "PC", outlet=OUTLET)
    _, tl = world.employee(hr)
    world.assign(tl, "TL")
    return emp["employeeId"], pc, tl


def half(world: World, part: str, day: date = TUE, name: str = "Diwali eve"):
    r = add(world, manager(world), day, name, dayPart=part)
    assert r.status_code == 201, r.text


def at(world: World, hour: int, minute: int = 0, day: date = TUE):
    world.clock.set(ist(day.year, day.month, day.day, hour, minute))


def test_morning_off_late_is_judged_from_the_split(world):
    _, pc, _ = pc_with_lead(world)
    half(world, "MORNING")
    at(world, 14, 40)
    today = world.client.get("/hr/v1/attendance/today", headers=world.headers(pc)).json()
    assert today["dayKind"] == "WORKING"
    assert today["halfDayHoliday"] == {
        "name": "Diwali eve",
        "dayPart": "MORNING",
        "splitAt": "14:00",
    }
    assert today["standardTimes"]["checkIn"] == "14:00"
    assert today["standardTimes"]["lateAfter"] == "14:45"
    assert send(world, pc, "in", token(world, pc)).json()["needsApproval"] == []

    _, other, _ = pc_with_lead(world)
    at(world, 14, 50)
    late = send(world, other, "in", token(world, other))
    assert late.json()["code"] == "ATTENDANCE_REASON_REQUIRED"
    assert "after 14:45" in late.json()["detail"]


def test_team_daily_carries_each_persons_times_moved_by_a_half_day_holiday(world):
    _, pc, tl = pc_with_lead(world)
    half(world, "MORNING")
    at(world, 14, 50)
    send(world, pc, "in", token(world, pc), reason="traffic")
    body = world.client.get(
        "/hr/v1/attendance/team-daily", params={"date": TUE.isoformat()}, headers=world.headers(tl)
    ).json()
    assert body["standardTimes"]["checkIn"] == "10:30"  # the company's ordinary day
    row = next(r for r in body["rows"] if r["checkInAt"])
    assert row["standardTimes"]["checkIn"] == "14:00"
    assert row["standardTimes"]["lateAfter"] == "14:45"
    assert row["standardTimes"]["minimumHours"] == 2.5
    assert [d["code"] for d in row["delinquencies"]] == ["LATE_CHECK_IN"]
    assert row["delinquencies"][0]["side"] == "CHECK_IN"


def test_afternoon_off_ends_the_day_at_the_split(world):
    _, pc, _ = pc_with_lead(world)
    half(world, "AFTERNOON")
    at(world, 10, 30)
    assert send(world, pc, "in", token(world, pc)).status_code == 200
    today = world.client.get("/hr/v1/attendance/today", headers=world.headers(pc)).json()
    assert today["standardTimes"]["checkOutEarliest"] == "14:00"
    assert today["standardTimes"]["absentAfter"] == "14:00"
    at(world, 13, 30)
    early = send(world, pc, "out", token(world, pc, "CHECK_OUT"))
    assert early.json()["code"] == "ATTENDANCE_REASON_REQUIRED"
    assert "before 14:00" in early.json()["detail"]
    at(world, 14, 5)
    out = send(world, pc, "out", token(world, pc, "CHECK_OUT"))
    assert out.status_code == 200 and out.json()["needsApproval"] == []

    _, late_comer, _ = pc_with_lead(world)
    at(world, 14, 10)
    closed = send(world, late_comer, "in", token(world, late_comer))
    assert closed.status_code == 422 and closed.json()["code"] == "ATTENDANCE_DAY_CLOSED"
    assert "past 14:00" in closed.json()["detail"] and "Diwali eve" in closed.json()["detail"]


def test_short_day_on_a_half_day_holiday_needs_half_the_hours_of_the_working_half(world):
    _, pc, _ = pc_with_lead(world)
    half(world, "AFTERNOON")
    at(world, 10, 30)
    send(world, pc, "in", token(world, pc))
    at(world, 14, 15)  # 3 h 30 m in the working half, more than 2.5 h
    assert send(world, pc, "out", token(world, pc, "CHECK_OUT")).json()["needsApproval"] == []

    _, short, _ = pc_with_lead(world)
    at(world, 12, 0)
    send(world, short, "in", token(world, short), reason="Bank")
    at(world, 14, 10)  # 2 h in the working half
    asked = send(world, short, "out", token(world, short, "CHECK_OUT"))
    assert asked.json()["code"] == "ATTENDANCE_REASON_REQUIRED"
    assert "2.5 hours" in asked.json()["detail"] and "working half" in asked.json()["detail"]
    r = send(world, short, "out", token(world, short, "CHECK_OUT"), reason="Unwell")
    assert r.json()["needsApproval"] == ["SHORT_DAY"]


def test_working_the_off_half_goes_to_the_team_lead_and_earns_half_a_comp_off(world):
    eid, pc, tl = pc_with_lead(world)
    setting(world, "leave.comp_off_from", "2026-11-01")
    half(world, "AFTERNOON")
    at(world, 10, 30)
    assert send(world, pc, "in", token(world, pc)).json()["needsApproval"] == []
    at(world, 18, 0)  # 3 h 30 m working half, 4 h of the holiday afternoon
    out = send(world, pc, "out", token(world, pc, "CHECK_OUT"))
    assert out.status_code == 200, out.text
    assert out.json()["needsApproval"] == ["OFF_DAY_WORK"] and "OFF_HALF" in out.json()["flags"]
    assert "EARLY" not in out.json()["flags"]
    items = world.client.get("/hr/v1/approvals/attendance", headers=world.headers(tl)).json()
    (item,) = items["items"]
    assert item["kind"] == "OFF_DAY_WORK" and item["event"] == "CHECK_OUT"
    done = world.client.post(
        f"/hr/v1/approvals/attendance/{item['exceptionId']}/decision",
        json={"decision": "APPROVE"},
        headers=world.headers(tl),
    )
    assert done.json()["status"] == "APPROVED"
    at(world, 10, 0, day=WED)
    b = world.client.get("/hr/v1/leave/balance?year=2026", headers=world.headers(pc)).json()
    comp = next(t for t in b["types"] if t["leaveType"] == "COMP_OFF")
    assert comp["balance"] == 0.5
    with world.engine.connect() as conn:
        note = conn.execute(
            text(
                "SELECT note FROM hr.leave_ledger WHERE employee_id = CAST(:e AS uuid)"
                " AND leave_type = 'COMP_OFF'"
            ),
            {"e": eid},
        ).scalar_one()
    assert note.startswith("Half a comp-off")


def test_morning_off_worked_from_nine_is_off_half_work_too(world):
    _, pc, _ = pc_with_lead(world)
    half(world, "MORNING")
    at(world, 9, 0)
    assert send(world, pc, "in", token(world, pc)).json()["needsApproval"] == []  # not late
    at(world, 18, 30)
    assert send(world, pc, "out", token(world, pc, "CHECK_OUT")).json()["needsApproval"] == [
        "OFF_DAY_WORK"
    ]


def test_a_little_time_in_the_off_half_is_not_holiday_work(world):
    _, pc, _ = pc_with_lead(world)
    half(world, "AFTERNOON")
    at(world, 10, 30)
    send(world, pc, "in", token(world, pc))
    at(world, 16, 29)  # 2 h 29 m of the afternoon
    assert send(world, pc, "out", token(world, pc, "CHECK_OUT")).json()["needsApproval"] == []


def test_a_full_holiday_for_one_project_is_a_day_off_only_there(world):
    eid, pc, _ = pc_with_lead(world)
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    _, elsewhere = world.employee(hr)
    world.assign(elsewhere, "PC", tenant="t-b", project=("B", "Bravo"), outlet=OUTLET)
    assert (
        add(
            world,
            manager(world),
            TUE,
            "Alpha day",
            appliesTo="SELECTED",
            scope=[{"tenantId": "tenant-a"}],
        ).status_code
        == 201
    )
    at(world, 10, 20)
    mine = world.client.get("/hr/v1/attendance/today", headers=world.headers(pc)).json()
    theirs = world.client.get("/hr/v1/attendance/today", headers=world.headers(elsewhere)).json()
    assert mine["dayKind"] == "HOLIDAY" and mine["holidayName"] == "Alpha day"
    assert theirs["dayKind"] == "WORKING" and theirs["halfDayHoliday"] is None
    assert send(world, pc, "in", token(world, pc)).json()["needsApproval"] == ["OFF_DAY_WORK"]
    assert send(world, elsewhere, "in", token(world, elsewhere)).json()["needsApproval"] == []


def test_calendar_and_hr_views_show_each_persons_own_holidays(world):
    eid, pc, _ = pc_with_lead(world)
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    other, other_user = world.employee(hr)
    world.assign(other_user, "PC", tenant="t-b", project=("B", "Bravo"), outlet=OUTLET)
    m = manager(world)
    add(world, m, TUE, "Diwali eve", dayPart="AFTERNOON")
    add(world, m, WED, "Alpha day", appliesTo="SELECTED", scope=[{"tenantId": "tenant-a"}])
    at(world, 18, 0, day=WED)
    cal = world.client.get(
        "/hr/v1/attendance/calendar/me?month=2026-11", headers=world.headers(pc)
    ).json()
    days = {d["date"]: d for w in cal["weeks"] for d in w["days"]}
    assert days["2026-11-10"]["dayKind"] == "WORKING"
    assert days["2026-11-10"]["halfDayHoliday"]["dayPart"] == "AFTERNOON"
    assert days["2026-11-10"]["status"] == "ABSENT"
    assert days["2026-11-11"]["dayKind"] == "HOLIDAY" and days["2026-11-11"]["status"] == "OFF_DAY"
    reader = world.grant(str(uuid.uuid4()), perm.HR_ATTENDANCE_READ_ALL)
    daily = world.client.get(
        "/hr/v1/attendance/daily?date=2026-11-11", headers=world.headers(reader)
    ).json()
    assert daily["dayKind"] == "WORKING"  # not a company-wide holiday
    assert daily["holidays"] == [
        {
            "name": "Alpha day",
            "dayPart": "FULL_DAY",
            "appliesTo": "SELECTED",
            "kind": "HOLIDAY",
            "scope": ["Project One"],
        }
    ]
    shown = {r["employeeId"]: r for r in daily["rows"]}
    assert eid not in shown  # a day off for them, with no attendance: not listed as absent
    assert shown[other["employeeId"]]["status"] == "ABSENT"
    month = world.client.get(
        "/hr/v1/attendance/team?month=2026-11", headers=world.headers(reader)
    ).json()
    rows = {e["employeeId"]: e for e in month["employees"]}
    # November 2026: 30 days, 5 Sundays; a half-day for everyone, a full day for Project One only
    assert month["summary"]["workingDays"] == 24.5
    assert rows[eid]["workingDays"] == 23.5
    assert rows[other["employeeId"]]["workingDays"] == 24.5
    assert month["summary"]["otherHolidays"][0]["name"] == "Alpha day"


# ---- leave and payroll -----------------------------------------------------------------------


def test_leave_across_a_half_day_holiday_counts_half_for_that_date(world):
    eid, pc, _ = pc_with_lead(world)
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hr.leave_ledger (employee_id, leave_type, leave_year, entry_type, days,"
                " note) VALUES (CAST(:e AS uuid), 'PAID', 2026, 'ADJUST', 5, 'test')"
            ),
            {"e": eid},
        )
    half(world, "AFTERNOON")
    with world.engine.connect() as conn:
        assert lr.leave_days(conn, date(2026, 11, 9), WED, False, eid) == Decimal("2.5")
        assert lr.leave_days(conn, TUE, TUE, False, eid) == Decimal("0.5")
        assert lr.leave_days(conn, WED, WED, True, eid) == Decimal("0.5")
    r = world.client.post(
        "/hr/v1/leave/requests",
        json={
            "leave_type": "PAID",
            "from_date": "2026-11-10",
            "to_date": "2026-11-10",
            "half_day": True,
        },
        headers=world.headers(pc),
    )
    assert r.status_code == 422 and r.json()["code"] == "LEAVE_HALF_DAY_ON_HALF_HOLIDAY"
    assert "afternoon off" in r.json()["detail"] and "full day" in r.json()["detail"]
    r = world.client.post(
        "/hr/v1/leave/requests",
        json={"leave_type": "PAID", "from_date": "2026-11-09", "to_date": "2026-11-11"},
        headers=world.headers(pc),
    )
    assert r.status_code == 201, r.text
    assert r.json()["days"] == 2.5


def test_a_scoped_full_holiday_is_not_a_leave_day_for_its_people_only(world):
    eid, _, _ = pc_with_lead(world)
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    other, other_user = world.employee(hr)
    world.assign(other_user, "PC", tenant="t-b", project=("B", "Bravo"), outlet=OUTLET)
    add(
        world,
        manager(world),
        TUE,
        "Alpha day",
        appliesTo="SELECTED",
        scope=[{"tenantId": "tenant-a"}],
    )
    with world.engine.connect() as conn:
        assert lr.leave_days(conn, date(2026, 11, 9), WED, False, eid) == Decimal(2)
        assert lr.leave_days(conn, date(2026, 11, 9), WED, False, other["employeeId"]) == Decimal(3)


def test_payroll_working_days_drop_by_half_and_present_days_follow(world):
    eid, pc, _ = pc_with_lead(world)
    half(world, "MORNING")
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hr.attendance_day (employee_id, work_date, check_in_at, check_out_at)"
                " VALUES (CAST(:e AS uuid), :d, :i, :o), (CAST(:e AS uuid), :d2, :i2, :o2)"
            ),
            {
                "e": eid,
                "d": TUE,
                "i": ist(2026, 11, 10, 14, 0),
                "o": ist(2026, 11, 10, 18, 30),
                "d2": WED,
                "i2": ist(2026, 11, 11, 10, 30),
                "o2": ist(2026, 11, 11, 18, 30),
            },
        )
        conn.execute(
            text(
                "INSERT INTO hr.leave_request (employee_id, leave_type, from_date, to_date, half_day,"
                " days, status, approver_rule, submitted_at) VALUES (CAST(:e AS uuid), 'UNPAID',"
                " :d, :d, false, 1, 'APPROVED', 'TL_PM', now())"
            ),
            {"e": eid, "d": THU},
        )
    month = date(2026, 11, 1)
    with world.engine.connect() as conn:
        holidays = pdata.declared_holidays(conn, month)[eid]
        assert pdata.working_days(month, holidays) == Decimal("24.5")
        assert pdata.working_days(month, {}) == Decimal(25)
        assert pdata.present_days(conn, eid, month, holidays) == Decimal("1.5")
        assert pdata.leave_days_in_month(conn, eid, month, holidays) == (Decimal(0), Decimal(1))
        # a day of leave on the half-day holiday counts only its working half
        conn.execute(text("UPDATE hr.leave_request SET from_date = :d, to_date = :d"), {"d": TUE})
        assert pdata.leave_days_in_month(conn, eid, month, holidays) == (Decimal(0), Decimal("0.5"))
        conn.rollback()


def test_an_attended_half_day_holiday_counts_half_a_day_towards_earned_leave(world):
    eid, _, _ = pc_with_lead(world)
    half(world, "AFTERNOON")
    add(world, manager(world), WED, "Full")
    with world.engine.begin() as conn:
        for d in (TUE, WED, THU):
            conn.execute(
                text(
                    "INSERT INTO hr.attendance_day (employee_id, work_date, check_in_at)"
                    " VALUES (CAST(:e AS uuid), :d, :i)"
                ),
                {"e": eid, "d": d, "i": ist(d.year, d.month, d.day, 10, 30)},
            )
    with world.engine.connect() as conn:
        units = lr.attendance_units(conn, eid, None, date(2026, 11, 13))
    # product owner, 7 Nov 2026: halves add up as halves; the full holiday earns a comp-off instead
    assert units == [(TUE, Decimal("0.5")), (THU, Decimal(1))]


def test_a_correction_is_refused_on_a_full_holiday_of_ones_own_project_only(world):
    from hrmgmt.api.attendance_corrections import _check_day
    from hrmgmt.errors import ApiError

    eid, _, _ = pc_with_lead(world)
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    other, other_user = world.employee(hr)
    world.assign(other_user, "PC", tenant="t-b", project=("B", "Bravo"), outlet=OUTLET)
    m = manager(world)
    add(world, m, TUE, "Alpha day", appliesTo="SELECTED", scope=[{"tenantId": "tenant-a"}])
    add(world, m, WED, "Eve", dayPart="MORNING")
    with world.engine.connect() as conn:
        with pytest.raises(ApiError, match="Alpha day is a declared holiday"):
            _check_day(conn, eid, TUE, THU)
        _check_day(conn, other["employeeId"], TUE, THU)  # a working day for them: allowed
        _check_day(conn, eid, WED, THU)  # a half-day holiday has a working half: allowed


def test_the_short_day_email_speaks_of_the_working_half():
    from hrmgmt.short_day import ShortDayNotice

    notice = ShortDayNotice(
        employee_name="Asha",
        employee_code="E1",
        work_date=TUE,
        check_in_at=ist(2026, 11, 10, 12, 0),
        check_out_at=ist(2026, 11, 10, 15, 0),
        minimum_hours=2.5,
        reason="Unwell",
        pm_user_ids=("pm",),
        company="Verigence",
        sign_in_link="",
        worked_in_half=timedelta(hours=2),
        half_day="Diwali eve, afternoon off from 14:00",
    )
    assert "2 h 00 m" in notice.subject()
    assert "worked 2 h 00 m of the working half" in notice.body("Priya")
    assert "less than the 2.5 hours required" in notice.body("Priya")


# ---- working Sundays (product owner, 7 Nov 2026) ------------------------------------------------

SUN = date(2026, 11, 15)


def other_project_pc(world: World):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    emp, user = world.employee(hr)
    world.assign(user, "PC", tenant="t-b", project=("B", "Bravo"), outlet=OUTLET)
    return emp["employeeId"], user


def working_sunday(world: World, day: date = SUN, **body):
    body.setdefault("appliesTo", "SELECTED")
    body.setdefault("scope", [{"tenantId": "tenant-a"}])
    return add(world, manager(world), day, "Stock audit Sunday", kind="WORKING_DAY", **body)


def test_a_working_sunday_is_a_whole_declared_sunday_and_clashes_are_refused(world):
    pc_with_lead(world)
    other_project_pc(world)
    hr = manager(world)
    weekday = add(world, hr, TUE, "Not a Sunday", kind="WORKING_DAY")
    assert weekday.status_code == 422 and "on a Sunday" in weekday.json()["detail"]
    assert add(world, hr, SUN, "Half", kind="WORKING_DAY", dayPart="MORNING").status_code == 422
    assert add(world, hr, SUN, "Maybe", kind="WORKING_DAY", status="TENTATIVE").status_code == 422
    made = working_sunday(world)
    assert made.status_code == 201, made.text
    assert made.json()["kind"] == "WORKING_DAY" and made.json()["dayPart"] == "FULL_DAY"
    clash = add(world, hr, SUN, "Puja", appliesTo="SELECTED", scope=[{"tenantId": "tenant-a"}])
    assert clash.status_code == 409 and "(working Sunday)" in clash.json()["detail"]
    everyone = add(world, hr, SUN, "Company picnic")
    assert everyone.status_code == 409
    elsewhere = add(world, hr, SUN, "Bravo puja", appliesTo="SELECTED", scope=[{"tenantId": "t-b"}])
    assert elsewhere.status_code == 201  # another project: no clash
    # earlier app versions never see a working Sunday in the list; the new screen asks for it
    old = world.client.get("/hr/v1/holidays?year=2026", headers=world.headers(hr)).json()
    assert "Stock audit Sunday" not in {i["name"] for i in old["items"]}
    new = world.client.get(
        "/hr/v1/holidays?year=2026&includeWorkingDays=true", headers=world.headers(hr)
    ).json()
    item = next(i for i in new["items"] if i["name"] == "Stock audit Sunday")
    assert item["kind"] == "WORKING_DAY" and item["scope"][0]["tenantId"] == "tenant-a"
    with world.engine.begin() as conn:  # the table refuses a malformed one too
        with pytest.raises(Exception, match="holiday_working_day_shape"):
            conn.execute(
                text(
                    "INSERT INTO hr.holiday (holiday_date, name, status, kind)"
                    " VALUES ('2026-11-16', 'Monday', 'DECLARED', 'WORKING_DAY')"
                )
            )


def test_a_working_sunday_is_an_ordinary_working_day_only_for_its_people(world):
    eid, pc, _ = pc_with_lead(world)
    other, other_user = other_project_pc(world)
    assert working_sunday(world).status_code == 201
    at(world, 10, 20, day=SUN)
    mine = world.client.get("/hr/v1/attendance/today", headers=world.headers(pc)).json()
    theirs = world.client.get("/hr/v1/attendance/today", headers=world.headers(other_user)).json()
    assert mine["dayKind"] == "WORKING" and mine["workingSunday"] == {"name": "Stock audit Sunday"}
    assert theirs["dayKind"] == "SUNDAY" and theirs["workingSunday"] is None
    assert send(world, pc, "in", token(world, pc)).json()["needsApproval"] == []  # not off-day work
    assert send(world, other_user, "in", token(world, other_user)).json()["needsApproval"] == [
        "OFF_DAY_WORK"
    ]
    at(world, 13, 0, day=SUN)  # early and short, as on any working day
    r = send(world, pc, "out", token(world, pc, "CHECK_OUT"), reason="Unwell")
    assert set(r.json()["needsApproval"]) == {"EARLY_CHECK_OUT", "SHORT_DAY"}
    with world.engine.connect() as conn:
        # leave days, payroll working days and earned leave all count it as a working day
        assert lr.leave_days(conn, date(2026, 11, 14), date(2026, 11, 16), False, eid) == 3
        assert lr.leave_days(conn, date(2026, 11, 14), date(2026, 11, 16), False, other) == 2
        month = date(2026, 11, 1)
        assert pdata.working_days(month, pdata.declared_holidays(conn, month)[eid]) == 26
        assert pdata.working_days(month, pdata.declared_holidays(conn, month)[other]) == 25
        units = lr.attendance_units(conn, eid, None, date(2026, 11, 16))
        assert units[-1][0] == SUN
    at(world, 18, 0, day=date(2026, 11, 16))
    cal = world.client.get(
        "/hr/v1/attendance/calendar/me?month=2026-11", headers=world.headers(other_user)
    ).json()
    days = {d["date"]: d for w in cal["weeks"] for d in w["days"]}
    assert days["2026-11-15"]["dayKind"] == "SUNDAY" and days["2026-11-15"]["workingSunday"] is None
    hr_month = world.client.get(
        "/hr/v1/attendance/team?month=2026-11",
        headers=world.headers(world.grant(str(uuid.uuid4()), perm.HR_ATTENDANCE_READ_ALL)),
    ).json()
    rows = {e["employeeId"]: e for e in hr_month["employees"]}
    assert rows[eid]["workingDays"] == 26 and rows[other]["workingDays"] == 25
    assert hr_month["summary"]["workingSundays"] == [
        {"date": "2026-11-15", "name": "Stock audit Sunday", "appliesTo": ["Project One"]}
    ]


def test_a_missed_working_sunday_is_an_absence_on_the_calendar(world):
    eid, pc, _ = pc_with_lead(world)
    assert working_sunday(world, appliesTo="ALL", scope=[]).status_code == 201
    at(world, 18, 0, day=date(2026, 11, 16))
    cal = world.client.get(
        "/hr/v1/attendance/calendar/me?month=2026-11", headers=world.headers(pc)
    ).json()
    sunday = next(d for w in cal["weeks"] for d in w["days"] if d["date"] == "2026-11-15")
    assert sunday["dayKind"] == "WORKING" and sunday["status"] == "ABSENT"
    assert sunday["workingSunday"] == {"name": "Stock audit Sunday"}


# ---- leave taken in the 6 weeks before a request (product owner, 7 Nov 2026) ----------------


def approved(world: World, eid: str, leave_type: str, first: date, last: date, half=False):
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hr.leave_request (employee_id, leave_type, from_date, to_date, half_day,"
                " days, status, approver_rule, submitted_at) VALUES (CAST(:e AS uuid), :t, :a, :b,"
                " :h, 1, 'APPROVED', 'TL_PM', now())"
            ),
            {"e": eid, "t": leave_type, "a": first, "b": last, "h": half},
        )


def test_recent_leave_counts_approved_paid_and_unpaid_days_in_the_six_weeks_before(world):
    eid, pc, tl = pc_with_lead(world)
    half(world, "AFTERNOON", day=date(2026, 11, 3), name="Eve")
    approved(world, eid, "PAID", date(2026, 11, 2), date(2026, 11, 4))  # Mon-Wed, Tue half off
    approved(world, eid, "UNPAID", date(2026, 11, 6), date(2026, 11, 6), half=True)
    approved(
        world, eid, "SPECIAL", date(2026, 9, 26), date(2026, 9, 29)
    )  # Sat-Tue; window from 28 Sep
    approved(world, eid, "UNPAID", date(2026, 11, 7), date(2026, 11, 8))  # Sat, Sun off
    approved(world, eid, "SICK", date(2026, 9, 1), date(2026, 9, 2))  # before the window
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hr.leave_request (employee_id, leave_type, from_date, to_date, half_day,"
                " days, status, approver_rule, submitted_at) VALUES (CAST(:e AS uuid), 'PAID',"
                " '2026-10-20', '2026-10-20', false, 1, 'PENDING', 'TL_PM', now())"
            ),
            {"e": eid},
        )
        conn.execute(
            text(
                "INSERT INTO hr.leave_ledger (employee_id, leave_type, leave_year, entry_type, days,"
                " note) VALUES (CAST(:e AS uuid), 'PAID', 2026, 'ADJUST', 5, 'test')"
            ),
            {"e": eid},
        )
    with world.engine.connect() as conn:
        recent = lr.recent_leave(conn, eid, date(2026, 11, 9), date(2026, 11, 9))
    assert recent == {
        "windowFrom": "2026-09-28",
        "windowTo": "2026-11-08",
        "paidDays": 4.5,  # 2.5 (the half-day holiday counts half) + 2 special in the window
        "unpaidDays": 1.5,  # a half day + Saturday (Sunday left out); pending ones never count
    }
    preview = world.client.get(
        "/hr/v1/leave/recent?fromDate=2026-11-09", headers=world.headers(pc)
    ).json()
    assert preview == recent
    applied = world.client.post(
        "/hr/v1/leave/requests",
        json={"leave_type": "PAID", "from_date": "2026-11-09", "to_date": "2026-11-09"},
        headers=world.headers(pc),
    )
    assert applied.status_code == 201, applied.text
    assert applied.json()["recentLeave"] == recent
    items = world.client.get("/hr/v1/approvals/leave", headers=world.headers(tl)).json()["items"]
    mine = next(i for i in items if i["fromDate"] == "2026-11-09")
    assert mine["recentLeave"] == recent
    reviewer = world.grant(str(uuid.uuid4()), perm.HR_LEAVE_REVIEW)
    detail = world.client.get(
        f"/hr/v1/leave/employee/{eid}", headers=world.headers(reviewer)
    ).json()
    pending = next(r for r in detail["requests"] if r["fromDate"] == "2026-11-09")
    assert pending["recentLeave"] == recent
    assert all("recentLeave" not in r for r in detail["requests"] if r["status"] == "APPROVED")
