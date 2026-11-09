"""Someone who has left shows in attendance up to their last day and not after, and Audit Core is
told who has left (product owner, 9 Nov 2026)."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from sqlalchemy import text

from hrmgmt import permissions as perm
from hrmgmt import workcontext as wc
from hrmgmt.leavers import leavers
from tests.support import OUTLET, World, ist


@pytest.fixture()
def world(migrated_engine):
    w = World(migrated_engine, now=ist(2026, 10, 21, 18, 0))  # a Wednesday, after 17:00
    w.clean_assignments()
    with migrated_engine.begin() as conn:
        for table in (
            "attendance_correction",
            "attendance_exception",
            "attendance_day",
            "capture_token",
        ):
            conn.execute(text(f"DELETE FROM hr.{table}"))
        conn.execute(text("DELETE FROM hr.holiday WHERE status = 'DECLARED'"))
        conn.execute(text("DELETE FROM hr.setting"))
    return w


def _left(world: World, emp: dict, status: str, last_day: str | None) -> None:
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hr.employee SET employment_status = :s WHERE employee_id = CAST(:e AS uuid)"
            ),
            {"s": status, "e": emp["employeeId"]},
        )
        if last_day:
            conn.execute(
                text(
                    "INSERT INTO hr.employee_status_change (employee_id, from_status, to_status,"
                    " effective_date, reason, requested_by, status)"
                    " VALUES (CAST(:e AS uuid), 'ACTIVE', :s, :d, 'Left the company', 'hr', 'APPROVED')"
                ),
                {"e": emp["employeeId"], "s": status, "d": last_day},
            )


def _people(world: World):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    keeper = world.grant(str(uuid.uuid4()), perm.HR_ATTENDANCE_READ_ALL, perm.HR_EMPLOYEE_READ)
    staying, staying_user = world.employee(hr, date_of_joining="2026-01-05")
    leaving, leaving_user = world.employee(hr, date_of_joining="2026-01-05")
    gone_long_ago, gone_user = world.employee(hr, date_of_joining="2026-01-05")
    for user in (staying_user, leaving_user, gone_user):
        world.assign(user, "PC", outlet=OUTLET)
    _left(world, leaving, "QUIT", "2026-10-14")
    _left(world, gone_long_ago, "TERMINATED", "2026-09-10")
    return keeper, staying, leaving, gone_long_ago


def _daily(world: World, keeper: str, day: str) -> set[str]:
    r = world.client.get(
        "/hr/v1/attendance/daily", params={"date": day}, headers=world.headers(keeper)
    )
    assert r.status_code == 200, r.text
    return {row["employeeCode"] for row in r.json()["rows"]}


def test_daily_view_keeps_a_leaver_up_to_the_last_day_only(world):
    keeper, staying, leaving, gone = _people(world)
    on_last_day = _daily(world, keeper, "2026-10-14")
    assert {staying["employeeCode"], leaving["employeeCode"]} <= on_last_day
    assert gone["employeeCode"] not in on_last_day
    after = _daily(world, keeper, "2026-10-15")
    assert staying["employeeCode"] in after and leaving["employeeCode"] not in after


def test_the_team_month_counts_a_leaver_up_to_the_last_day(world):
    keeper, staying, leaving, gone = _people(world)
    body = world.client.get(
        "/hr/v1/attendance/team", params={"month": "2026-10"}, headers=world.headers(keeper)
    ).json()
    people = {e["employeeCode"]: e for e in body["employees"]}
    assert gone["employeeCode"] not in people
    # 1-14 Oct: 12 working days (two Sundays), all absent
    assert people[leaving["employeeCode"]]["workingDays"] == 12
    assert people[leaving["employeeCode"]]["daysAbsent"] == 12
    assert people[staying["employeeCode"]]["workingDays"] > 12


def test_the_week_shows_the_days_after_leaving_as_left(world):
    keeper, staying, leaving, gone = _people(world)
    body = world.client.get(
        "/hr/v1/attendance/team-weekly",
        params={"date": "2026-10-14"},
        headers=world.headers(keeper),
    ).json()
    people = {p["employeeCode"]: p for proj in body["projects"] for p in proj["people"]}
    assert gone["employeeCode"] not in people
    week = [d["status"] for d in people[leaving["employeeCode"]]["days"]]
    assert week[:3] == ["ABSENT", "ABSENT", "ABSENT"]  # 12-14 Oct
    assert week[3:] == ["LEFT"] * 4
    assert people[leaving["employeeCode"]]["days"][3]["label"] == "After leaving"


def test_audit_core_is_told_who_has_left(world):
    _, staying, leaving, gone = _people(world)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"leavers": 2, "rolesEnded": 1})

    client = wc.WorkContextClient(
        base_url="http://audit.test",
        token_provider=lambda audience: f"token-for-{audience}",
        transport=httpx.MockTransport(handler),
    )
    assert wc.report_leavers(world.engine, client) is True
    (call,) = calls
    assert call.method == "PUT" and call.url.path == "/v1/service/hr/employment"
    assert call.headers["Authorization"] == "Bearer token-for-audit"
    sent = {p["employeeCode"]: p for p in json.loads(call.content)["leavers"]}
    assert sent[leaving["employeeCode"]]["status"] == "QUIT"
    assert sent[leaving["employeeCode"]]["exitDate"] == "2026-10-14"
    assert sent[gone["employeeCode"]]["exitDate"] == "2026-09-10"
    assert staying["employeeCode"] not in sent
    with world.engine.connect() as conn:
        assert {p["employeeCode"] for p in leavers(conn)} >= {leaving["employeeCode"]}


def test_a_failed_report_is_only_logged(world):
    client = wc.WorkContextClient(
        base_url="http://audit.test",
        token_provider=lambda audience: "t",
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    assert wc.report_leavers(world.engine, client) is False


def test_an_approved_status_change_tells_audit_core(world):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE, perm.HR_EMPLOYEE_READ)
    ceo = world.grant(
        str(uuid.uuid4()),
        perm.HR_EMPLOYEE_MANAGE,
        perm.HR_EMPLOYEE_READ,
        perm.HR_EMPLOYEE_STATUS_APPROVE,
    )
    emp, _ = world.employee(hr)
    calls: list[httpx.Request] = []
    world.app.state.workcontext = wc.WorkContextClient(
        base_url="http://audit.test",
        token_provider=lambda audience: "t",
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200, json={"rolesEnded": 0})
        ),
    )
    try:
        asked = world.client.post(
            f"/hr/v1/employees/{emp['employeeId']}/status-change",
            json={"to_status": "QUIT", "reason": "Resigned and served notice"},
            headers=world.headers(hr),
        )
        assert asked.status_code == 201, asked.text
        assert calls == []  # nothing changes until it is approved
        approved = world.client.post(
            f"/hr/v1/employee-status-changes/{asked.json()['changeId']}/approve",
            json={},
            headers=world.headers(ceo),
        )
        assert approved.status_code == 200, approved.text
        assert len(calls) == 1
        sent = json.loads(calls[0].content)["leavers"]
        assert emp["employeeCode"] in {p["employeeCode"] for p in sent}
    finally:
        world.app.state.workcontext = None
