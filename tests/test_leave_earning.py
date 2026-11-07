"""Paid leave earned from attendance, Special occasion leave, comp-off, unexplained absences, the
outlet cover warning and the Executive view of what waits with each Team Lead (4 Nov 2026)."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import text

from hrmgmt import permissions as perm
from hrmgmt.api.oversight import deadline_for
from hrmgmt.timeutil import IST
from tests.support import OUTLET, World, ist
from tests.test_leave import apply, balance, decide, person


@pytest.fixture()
def world(migrated_engine):
    w = World(migrated_engine, now=ist(2026, 12, 1, 10, 0))  # Tuesday 1 Dec 2026
    w.clean_assignments()
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE hr.leave_ledger, hr.leave_request, hr.leave_accrual,"
                " hr.attendance_exception, hr.attendance_day CASCADE"
            )
        )
        conn.execute(text("DELETE FROM hr.setting"))
        conn.execute(text("DELETE FROM hr.holiday WHERE status = 'DECLARED'"))
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


def working_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() != 6:
            out.append(d)
        d += timedelta(days=1)
    return out


def attend(
    world: World, employee_id: str, days, exception: str | None = None, kind="LATE_CHECK_IN"
):
    """Attendance on each day; with `exception`, a late check-in in that status on it."""
    ids = []
    with world.engine.begin() as conn:
        for d in days:
            aid = conn.execute(
                text(
                    "INSERT INTO hr.attendance_day (employee_id, work_date, check_in_at)"
                    " VALUES (CAST(:e AS uuid), :d, :t) RETURNING attendance_id"
                ),
                {"e": employee_id, "d": d, "t": datetime(d.year, d.month, d.day, 10, tzinfo=IST)},
            ).scalar_one()
            if exception:
                ids.append(
                    str(
                        conn.execute(
                            text(
                                "INSERT INTO hr.attendance_exception (attendance_id, employee_id,"
                                " work_date, event, kind, status, decided_at) VALUES (:a,"
                                " CAST(:e AS uuid), :d, 'CHECK_IN', :k, :s,"
                                " CASE WHEN :s = 'PENDING' THEN NULL ELSE now() END)"
                                " RETURNING exception_id"
                            ),
                            {"a": aid, "e": employee_id, "d": d, "k": kind, "s": exception},
                        ).scalar_one()
                    )
                )
    return ids


def earning(world: World, user: str) -> dict:
    return world.client.get("/hr/v1/leave/earning", headers=world.headers(user)).json()


def test_one_paid_day_on_completing_22_days_of_attendance(world):
    emp, user = person(world, paid=0)
    days = working_days(date(2026, 10, 1), 22)
    attend(world, emp["employeeId"], days[:21])
    assert balance(world, user)["balance"] == 0.0
    assert earning(world, user)["attendanceDaysSoFar"] == 21.0
    attend(world, emp["employeeId"], days[21:])
    assert balance(world, user)["balance"] == 1.0
    e = earning(world, user)
    assert e["attendanceDaysSoFar"] == 0.0
    assert e["blocks"] == [
        {
            "blockNo": 1,
            "completedOn": days[21].isoformat(),
            "attendanceDays": 22.0,
            "credited": 1.0,
            "lapsed": 0.0,
        }
    ]
    assert balance(world, user)["balance"] == 1.0  # reading again earns nothing more


def test_today_and_sundays_do_not_count_towards_the_22(world):
    emp, user = person(world, paid=0)
    setting(world, "leave.accrual_attendance_days", 2)
    attend(world, emp["employeeId"], [date(2026, 11, 29)])  # a Sunday
    attend(world, emp["employeeId"], [date(2026, 11, 30), date(2026, 12, 1)])  # Mon, today
    assert balance(world, user)["balance"] == 0.0
    assert earning(world, user)["attendanceDaysSoFar"] == 1.0


def test_the_cap_stops_earning_and_what_it_stops_is_recorded_as_lapsed(world):
    emp, user = person(world, "PC", paid=0)
    _, tl = person(world, "TL", paid=0)
    setting(world, "leave.accrual_attendance_days", 2)
    attend(world, emp["employeeId"], working_days(date(2026, 11, 2), 8))  # four blocks
    assert balance(world, user)["balance"] == 3.0
    blocks = earning(world, user)["blocks"]
    assert [b["credited"] for b in blocks] == [0.0, 1.0, 1.0, 1.0]  # newest first
    assert blocks[0]["lapsed"] == 1.0
    # once a day is used, the next block earns again
    rid = apply(world, user, from_date="2026-12-02").json()["requestId"]
    decide(world, tl, rid)
    attend(world, emp["employeeId"], working_days(date(2026, 11, 12), 2))
    assert balance(world, user)["balance"] == 3.0
    assert earning(world, user)["blocks"][0]["credited"] == 1.0


def test_a_rejected_exception_makes_the_day_half_and_a_waiting_one_pauses_the_count(world):
    emp, user = person(world, paid=0)
    setting(world, "leave.accrual_attendance_days", 2)
    d = working_days(date(2026, 11, 2), 5)
    attend(world, emp["employeeId"], [d[0]], exception="REJECTED")  # 0.5
    waiting = attend(world, emp["employeeId"], [d[1]], exception="PENDING")
    attend(world, emp["employeeId"], d[2:])
    assert balance(world, user)["balance"] == 0.0
    e = earning(world, user)
    assert e["attendanceDaysSoFar"] == 0.5 and e["countPausedFrom"] == d[1].isoformat()
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.attendance_exception SET status = 'APPROVED', decided_at = now()"
                " WHERE exception_id = CAST(:x AS uuid)"
            ),
            {"x": waiting[0]},
        )
    # 0.5 + 1 + 1 = 2.5: one block, 0.5 carried; then 1 + 1 = 2: a second block
    assert balance(world, user)["balance"] == 2.0
    blocks = earning(world, user)["blocks"]
    assert [b["completedOn"] for b in blocks] == [d[4].isoformat(), d[2].isoformat()]
    assert earning(world, user)["attendanceDaysSoFar"] == 0.5


def test_special_occasion_leave_is_birthday_leave_with_its_own_quota(world):
    _, pc = person(world, "PC", paid=0)  # born 30 Aug: this window runs 30 Aug 2026 to 29 Aug 2027
    _, tl = person(world, "TL", paid=0)
    r = apply(
        world,
        pc,
        leave_type="SPECIAL",
        from_date="2026-12-07",
        to_date="2026-12-08",
        occasion="Sister's wedding",  # what earlier app versions send is not kept
    )
    assert r.status_code == 201 and r.json()["occasion"] == "Birthday"
    assert r.json()["approverRule"] == "TL_PM"
    assert decide(world, tl, r.json()["requestId"]).json()["status"] == "APPROVED"
    more = apply(world, pc, leave_type="SPECIAL", from_date="2026-12-09")
    assert more.status_code == 409 and more.json()["code"] == "LEAVE_BALANCE_TOO_LOW"
    assert "opens on your birthday, 30 Aug 2027" in more.json()["detail"]
    # paid leave is untouched by it
    assert balance(world, pc)["balance"] == 0.0


def test_approved_off_day_work_earns_one_comp_off_once_from_the_start_date(world):
    emp, user = person(world, paid=0)
    setting(world, "leave.comp_off_from", "2026-11-01")
    with world.engine.begin() as conn:
        conn.execute(text("SELECT 1"))
    attend(world, emp["employeeId"], [date(2026, 11, 8)], exception="APPROVED", kind="OFF_DAY_WORK")
    attend(world, emp["employeeId"], [date(2026, 11, 15)], exception="PENDING", kind="OFF_DAY_WORK")
    with world.engine.begin() as conn:
        # approved before the rule began: not credited
        old = attend(
            world,
            emp["employeeId"],
            [date(2026, 10, 25)],
            exception="APPROVED",
            kind="OFF_DAY_WORK",
        )
        conn.execute(
            text(
                "UPDATE hr.attendance_exception SET decided_at = '2026-10-26T10:00:00+05:30'"
                " WHERE exception_id = CAST(:x AS uuid)"
            ),
            {"x": old[0]},
        )
    assert balance(world, user, "COMP_OFF")["balance"] == 1.0
    assert balance(world, user, "COMP_OFF")["balance"] == 1.0  # never twice
    assert balance(world, user)["balance"] == 0.0  # Sundays do not count towards paid leave


def test_unexplained_absences_can_be_turned_into_leave(world):
    emp, user = person(world)
    world.clock.set(ist(2026, 11, 10, 10, 0))  # Tuesday
    attend(world, emp["employeeId"], [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)])
    apply(world, user, from_date="2026-11-05")
    r = world.client.get("/hr/v1/leave/absences", headers=world.headers(user)).json()
    assert r["dates"] == ["2026-11-06", "2026-11-07", "2026-11-09"]  # 8 Nov is a Sunday
    assert apply(world, user, from_date="2026-11-06", to_date="2026-11-07").status_code == 201
    r = world.client.get("/hr/v1/leave/absences", headers=world.headers(user)).json()
    assert r["dates"] == ["2026-11-09"]
    _, newcomer = person(world)
    assert world.client.get("/hr/v1/leave/absences", headers=world.headers(newcomer)).json() == {
        "dates": []
    }


def test_the_team_lead_is_warned_when_a_leave_would_leave_an_outlet_without_a_pc(world):
    outlet = str(uuid.uuid4())
    _, pc1 = person(world, "PC", outlet_id=outlet)
    _, pc2 = person(world, "PC", outlet_id=outlet)
    _, tl = person(world, "TL")
    other = apply(world, pc2, from_date="2026-12-08").json()["requestId"]
    decide(world, tl, other)
    rid = apply(world, pc1, from_date="2026-12-07", to_date="2026-12-08").json()["requestId"]
    items = world.client.get("/hr/v1/approvals/leave", headers=world.headers(tl)).json()["items"]
    mine = next(i for i in items if i["requestId"] == rid)
    assert mine["coverWarnings"] == [
        {"outletName": OUTLET[0], "otherPcs": 1, "dates": ["2026-12-08"]}
    ]
    assert mine["balanceAvailable"] == 5.0
    half = apply(world, pc1, from_date="2026-12-10", half_day=True).json()["requestId"]
    items = world.client.get("/hr/v1/approvals/leave", headers=world.headers(tl)).json()["items"]
    assert next(i for i in items if i["requestId"] == half)["coverWarnings"] == []


def test_the_deadline_is_monday_0900_after_the_week_it_was_raised_in():
    assert deadline_for(datetime(2026, 11, 4, 22, 0, tzinfo=IST)) == datetime(
        2026, 11, 9, 9, 0, tzinfo=IST
    )
    assert deadline_for(datetime(2026, 11, 8, 23, 59, tzinfo=IST)) == datetime(
        2026, 11, 9, 9, 0, tzinfo=IST
    )
    assert deadline_for(datetime(2026, 11, 9, 0, 5, tzinfo=IST)) == datetime(
        2026, 11, 16, 9, 0, tzinfo=IST
    )


def test_an_executive_sees_what_waits_with_each_team_lead_of_their_project(world):
    emp, pc = person(world, "PC")
    _, tl = person(world, "TL")
    _, executive = person(world, "EXECUTIVE")
    _, outsider_tl = person(world, "TL", tenant="tenant-b")
    _, other_exec = person(world, "EXECUTIVE", tenant="tenant-b")
    old = attend(world, emp["employeeId"], [date(2026, 11, 20)], exception="PENDING")
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.attendance_exception SET created_at = '2026-11-20T11:30:00+05:30'"
                " WHERE exception_id = CAST(:x AS uuid)"
            ),
            {"x": old[0]},
        )
    apply(world, pc, from_date="2026-12-07")
    r = world.client.get("/hr/v1/oversight/approvers", headers=world.headers(executive))
    assert r.status_code == 200
    rows = {i["userId"]: i for i in r.json()["items"]}
    assert set(rows) == {tl}  # only their project's Team Lead
    assert rows[tl]["pending"] == {"ATTENDANCE": 1, "LEAVE": 1, "CLAIM": 0, "total": 2}
    assert rows[tl]["overdue"] == 1  # raised the week of 16 Nov, due 23 Nov 09:00
    detail = world.client.get(
        f"/hr/v1/oversight/approvers/{tl}/pending", headers=world.headers(executive)
    ).json()["items"]
    assert [i["type"] for i in detail] == ["ATTENDANCE", "LEAVE"]
    assert detail[0]["overdue"] and not detail[1]["overdue"]
    # another project's Executive does not see this Team Lead
    assert (
        world.client.get(
            f"/hr/v1/oversight/approvers/{tl}/pending", headers=world.headers(other_exec)
        ).status_code
        == 403
    )
    # a PC is not allowed; HR sees every project
    assert (
        world.client.get("/hr/v1/oversight/approvers", headers=world.headers(pc)).status_code == 403
    )
    hr = world.grant(str(uuid.uuid4()), perm.HR_ATTENDANCE_READ_ALL)
    everyone = world.client.get("/hr/v1/oversight/approvers", headers=world.headers(hr)).json()
    assert {i["userId"] for i in everyone["items"]} == {tl, outsider_tl}
    # deciding shows in the decision time
    decide(
        world,
        tl,
        world.client.get("/hr/v1/approvals/leave", headers=world.headers(tl)).json()["items"][0][
            "requestId"
        ],
    )
    rows = {
        i["userId"]: i
        for i in world.client.get(
            "/hr/v1/oversight/approvers", headers=world.headers(executive)
        ).json()["items"]
    }
    assert rows[tl]["pending"]["total"] == 1 and rows[tl]["decidedLast30Days"] == 1
