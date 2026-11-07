"""Special occasion leave is birthday leave (product owner, 7 Nov 2026)."""

from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from sqlalchemy import text

from hrmgmt import leave_rules as lr
from hrmgmt import permissions as perm
from tests.support import World, ist
from tests.test_leave import apply, balance, decide, person


@pytest.fixture()
def world(migrated_engine):
    w = World(migrated_engine, now=ist(2027, 8, 1, 10, 0))
    w.clean_assignments()
    with migrated_engine.begin() as conn:
        conn.execute(text("TRUNCATE hr.leave_ledger, hr.leave_request, hr.leave_accrual CASCADE"))
        conn.execute(text("DELETE FROM hr.setting"))
        conn.execute(text("DELETE FROM hr.holiday WHERE status = 'DECLARED'"))
        conn.execute(
            text("INSERT INTO hr.setting (key, value) VALUES (:k, CAST(:v AS jsonb))"),
            {"k": "leave.special_days_per_year", "v": json.dumps(1)},
        )
    return w


def birthday(world, user, day: str):
    return apply(world, user, leave_type="SPECIAL", from_date=day, to_date=day)


def team(world, dob: str | None = "1990-08-30"):
    emp, pc = person(world, "PC", paid=0, dob=dob)
    _, tl = person(world, "TL", paid=0)
    return emp["employeeId"], pc, tl


def test_the_window_runs_from_a_birthday_to_the_day_before_the_next():
    dob = date(1990, 8, 30)
    assert lr.birthday_window(dob, date(2027, 8, 29)) == (
        2026,
        date(2026, 8, 30),
        date(2027, 8, 29),
    )
    assert lr.birthday_window(dob, date(2027, 8, 30)) == (
        2027,
        date(2027, 8, 30),
        date(2028, 8, 29),
    )
    leap = date(1992, 2, 29)
    assert lr.birthday_in(leap, 2027) == date(2027, 2, 28)
    assert lr.birthday_in(leap, 2028) == date(2028, 2, 29)
    assert lr.birthday_window(leap, date(2027, 2, 27)) == (
        2026,
        date(2026, 2, 28),
        date(2027, 2, 27),
    )
    assert lr.birthday_window(leap, date(2028, 2, 28)) == (
        2027,
        date(2027, 2, 28),
        date(2028, 2, 28),
    )


def test_taken_later_in_the_window_then_refused_before_the_next_birthday_then_on_it(world):
    eid, pc, tl = team(world)
    later = birthday(world, pc, "2027-08-16")  # the leave of 30 Aug 2026, not taken on the day
    assert later.status_code == 201, later.text
    assert later.json()["occasion"] == "Birthday"
    assert decide(world, tl, later.json()["requestId"]).json()["status"] == "APPROVED"
    before = birthday(world, pc, "2027-08-27")
    assert before.status_code == 409
    assert before.json()["detail"].endswith("Birthday leave opens on your birthday, 30 Aug 2027.")
    on_the_day = birthday(world, pc, "2027-08-30")  # a new birthday opens a new one
    assert on_the_day.status_code == 201, on_the_day.text
    assert decide(world, tl, on_the_day.json()["requestId"]).json()["status"] == "APPROVED"
    again = birthday(world, pc, "2027-09-02")
    assert again.status_code == 409 and "30 Aug 2028" in again.json()["detail"]
    with world.engine.connect() as conn:
        years = conn.execute(
            text(
                "SELECT leave_year, entry_type, days FROM hr.leave_ledger"
                " WHERE employee_id = CAST(:e AS uuid) AND leave_type = 'SPECIAL'"
                " ORDER BY created_at, leave_year"
            ),
            {"e": eid},
        ).all()
    assert {(y, t, float(d)) for y, t, d in years} == {
        (2026, "GRANT", 1.0),
        (2026, "DEDUCT", -1.0),
        (2027, "GRANT", 1.0),
        (2027, "DEDUCT", -1.0),
    }


def test_one_birthdays_leave_does_not_carry_into_the_next(world):
    _, pc, _ = team(world)
    # the leave of 30 Aug 2026 was never taken; 30 Aug 2027 opens one day, not two
    assert (
        apply(world, pc, leave_type="SPECIAL", from_date="2027-08-30", to_date="2027-08-31").json()[
            "code"
        ]
        == "LEAVE_BALANCE_TOO_LOW"
    )
    assert birthday(world, pc, "2027-08-31").status_code == 201


def test_dates_running_across_the_birthday_are_refused(world):
    _, pc, _ = team(world)
    r = apply(world, pc, leave_type="SPECIAL", from_date="2027-08-27", to_date="2027-08-30")
    assert r.status_code == 422 and r.json()["code"] == "LEAVE_BIRTHDAY_NOT_OPEN"
    assert "Birthday leave opens on your birthday, 30 Aug 2027." in r.json()["detail"]


def test_a_29_february_birthday_opens_on_28_february_in_other_years(world):
    world.clock.set(ist(2027, 2, 20, 10, 0))
    _, pc, _ = team(world, dob="1992-02-29")
    first = birthday(world, pc, "2027-02-26")  # the leave of 28 Feb 2026
    assert first.status_code == 201, first.text
    refused = birthday(world, pc, "2027-02-27")
    assert refused.status_code == 409
    assert "opens on your birthday, 28 Feb 2027" in refused.json()["detail"]
    assert birthday(world, pc, "2027-03-01").status_code == 201


def test_no_date_of_birth_means_no_birthday_leave(world):
    _, pc, _ = team(world, dob=None)
    r = birthday(world, pc, "2027-08-30")
    assert r.status_code == 422 and r.json()["detail"] == lr.NO_DATE_OF_BIRTH
    b = world.client.get("/hr/v1/leave/balance", headers=world.headers(pc)).json()
    assert b["birthdayLeave"] == {"dateOfBirthOnFile": False, "message": lr.NO_DATE_OF_BIRTH}
    assert balance(world, pc, "SPECIAL")["balance"] == 0.0


def test_the_balance_card_says_which_birthday_window_it_is(world):
    _, pc, _ = team(world)
    b = world.client.get("/hr/v1/leave/balance", headers=world.headers(pc)).json()
    assert b["birthdayLeave"] == {
        "dateOfBirthOnFile": True,
        "birthdayYear": 2026,
        "availableFrom": "2026-08-30",
        "availableUntil": "2027-08-29",
        "nextOpensOn": "2027-08-30",
    }
    special = balance(world, pc, "SPECIAL")
    assert special["balance"] == 1.0 and special["available"] == 1.0
    assert birthday(world, pc, "2027-08-02").status_code == 201
    assert balance(world, pc, "SPECIAL")["available"] == 0.0  # waiting counts against it


def test_approval_checks_the_birthday_rule_again(world):
    eid, pc, tl = team(world)
    waiting = birthday(world, pc, "2027-08-16").json()["requestId"]
    other = birthday(world, pc, "2027-09-06").json()["requestId"]  # next birthday's leave
    hr = world.grant(str(uuid.uuid4()), perm.HR_LEAVE_REVIEW)
    # HR takes the 2026 birthday's day off by an adjustment: the waiting request no longer fits
    adjusted = world.client.post(
        f"/hr/v1/leave/employee/{eid}/adjust",
        json={"leave_type": "SPECIAL", "year": 2026, "days": -1, "note": "taken outside myHR"},
        headers=world.headers(hr),
    )
    assert adjusted.status_code in (200, 201), adjusted.text
    refused = decide(world, tl, waiting)
    assert refused.status_code == 409
    assert "Birthday leave opens on your birthday, 30 Aug 2027." in refused.json()["detail"]
    # the date of birth is removed: refused with the same words the employee would see
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.employee SET date_of_birth = NULL WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"e": eid},
        )
    gone = decide(world, tl, other)
    assert gone.status_code == 422 and gone.json()["detail"] == lr.NO_DATE_OF_BIRTH


def test_recent_leave_counts_only_days_already_taken(world):
    eid, pc, tl = team(world)
    world.clock.set(ist(2026, 11, 9, 10, 0))
    with world.engine.begin() as conn:
        for first, last in (("2026-11-02", "2026-11-03"), ("2026-11-20", "2026-11-20")):
            conn.execute(
                text(
                    "INSERT INTO hr.leave_request (employee_id, leave_type, from_date, to_date,"
                    " half_day, days, status, approver_rule, submitted_at) VALUES"
                    " (CAST(:e AS uuid), 'UNPAID', :a, :b, false, 1, 'APPROVED', 'TL_PM', now())"
                ),
                {"e": eid, "a": first, "b": last},
            )
    later = world.client.get(
        "/hr/v1/leave/recent?fromDate=2026-11-30", headers=world.headers(pc)
    ).json()
    # ends yesterday (8 Nov), not the day before the leave: 20 Nov is approved but not yet taken
    assert later == {
        "windowFrom": "2026-09-28",
        "windowTo": "2026-11-08",
        "paidDays": 0.0,
        "unpaidDays": 2.0,
    }
    earlier = world.client.get(
        "/hr/v1/leave/recent?fromDate=2026-11-03", headers=world.headers(pc)
    ).json()
    assert earlier["windowTo"] == "2026-11-02" and earlier["unpaidDays"] == 1.0
