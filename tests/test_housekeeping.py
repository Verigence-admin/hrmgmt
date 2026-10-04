from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from hrmgmt import permissions as perm
from tests.support import NEAR, OUTLET, World, ist
from tests.test_attendance import send, token

URL = "/hr/v1/housekeeping"


@pytest.fixture()
def world(migrated_engine):
    w = World(migrated_engine, now=ist(2026, 11, 16, 10, 0))
    w.clean_assignments()
    with migrated_engine.begin() as conn:
        conn.execute(text("TRUNCATE hr.leave_ledger, hr.leave_request"))
        for table in ("attendance_exception", "attendance_day", "capture_token"):
            conn.execute(text(f"DELETE FROM hr.{table}"))
        conn.execute(text("DELETE FROM hr.claim_event"))
        conn.execute(text("DELETE FROM hr.claim_receipt"))
        conn.execute(text("DELETE FROM hr.claim"))
        conn.execute(text("DELETE FROM hr.payroll_line"))
        conn.execute(text("DELETE FROM hr.payroll_run"))
        conn.execute(text("DELETE FROM hr.setting"))
    return w


def _keeper(world: World) -> str:
    return world.grant(str(uuid.uuid4()), perm.HR_HOUSEKEEPING_MANAGE)


def _person(world: World):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    emp, user = world.employee(hr)
    world.assign(user, "PC", outlet=OUTLET)
    return emp, user


def _scope(kind: str, a: str, b: str, **more):
    return {"kind": kind, "from_date": a, "to_date": b, **more}


def _count(engine, sql: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(sql)).scalar_one())


def _leave(engine, emp_id: str, day: str, status="APPROVED") -> str:
    with engine.begin() as conn:
        rid = conn.execute(
            text(
                "INSERT INTO hr.leave_request (employee_id, leave_type, from_date, to_date, days,"
                " status, approver_rule) VALUES (CAST(:e AS uuid), 'SICK', :d, :d, 1, :s, 'HR')"
                " RETURNING request_id"
            ),
            {"e": emp_id, "d": day, "s": status},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO hr.leave_ledger (employee_id, leave_type, leave_year, entry_type, days,"
                " request_id) VALUES (CAST(:e AS uuid), 'SICK', 2026, 'DEDUCT', -1, :r)"
            ),
            {"e": emp_id, "r": rid},
        )
    return str(rid)


def _claim(engine, emp_id: str, day: str, status="SUBMITTED", month="2026-10-01") -> str:
    with engine.begin() as conn:
        cid = conn.execute(
            text(
                "INSERT INTO hr.claim (employee_id, category_code, expense_date, amount, status,"
                " payroll_month) VALUES (CAST(:e AS uuid), 'BIKE_TAXI', :d, 100, :s, :m)"
                " RETURNING claim_id"
            ),
            {"e": emp_id, "d": day, "s": status, "m": month},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO hr.claim_receipt (claim_id, file_key, content_type, size_bytes)"
                " VALUES (:c, :k, 'image/jpeg', 3)"
            ),
            {"c": cid, "k": f"claims/{cid}/r.jpg"},
        )
        conn.execute(
            text("INSERT INTO hr.claim_event (claim_id, event_type) VALUES (:c, 'SUBMITTED')"),
            {"c": cid},
        )
    return str(cid)


def _run(engine, month: str, status: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hr.payroll_run (pay_month, status, statutory_config, created_by)"
                " VALUES (:m, :s, '{}'::jsonb, 'test')"
            ),
            {"m": month, "s": status},
        )


def test_only_the_housekeeping_permission_may_use_it(world):
    nobody = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE, perm.HR_SETTINGS_MANAGE)
    body = _scope("ATTENDANCE", "2026-11-15", "2026-11-15")
    for path in ("/preview", "/purge"):
        r = world.client.post(
            URL + path, json={**body, "confirm": "DELETE"}, headers=world.headers(nobody)
        )
        assert r.status_code in (403, 404), r.text


def test_preview_counts_and_changes_nothing(world, migrated_engine):
    keeper = _keeper(world)
    emp, user = _person(world)
    assert send(world, user, "in", token(world, user), where=NEAR).status_code == 200
    world.clock.set(ist(2026, 11, 16, 10, 0))
    r = world.client.post(
        URL + "/preview",
        json=_scope("ATTENDANCE", "2026-11-16", "2026-11-16"),
        headers=world.headers(keeper),
    )
    assert r.status_code == 200, r.text
    assert r.json()["counts"] == {"days": 1, "approvals": 0, "photos": 1}
    assert _count(migrated_engine, "SELECT count(*) FROM hr.attendance_day") == 1


def test_attendance_purge_needs_the_word_and_removes_days_exceptions_and_photos(
    world, migrated_engine
):
    keeper = _keeper(world)
    emp, user = _person(world)
    assert send(world, user, "in", token(world, user), where=NEAR).status_code == 200
    stored = list(world.storage.objects)
    assert len(stored) == 1
    body = _scope("ATTENDANCE", "2026-11-16", "2026-11-16")
    refused = world.client.post(
        URL + "/purge", json={**body, "confirm": "delete"}, headers=world.headers(keeper)
    )
    assert refused.status_code == 422 and refused.json()["code"] == "HOUSEKEEPING_NOT_CONFIRMED"
    assert _count(migrated_engine, "SELECT count(*) FROM hr.attendance_day") == 1
    other_day = world.client.post(
        URL + "/purge",
        json={**_scope("ATTENDANCE", "2026-11-15", "2026-11-15"), "confirm": "DELETE"},
        headers=world.headers(keeper),
    )
    assert other_day.json()["counts"]["days"] == 0
    assert _count(migrated_engine, "SELECT count(*) FROM hr.attendance_day") == 1
    done = world.client.post(
        URL + "/purge", json={**body, "confirm": "DELETE"}, headers=world.headers(keeper)
    )
    assert done.status_code == 200, done.text
    assert done.json()["counts"]["days"] == 1 and done.json()["filesNotRemoved"] == 0
    assert _count(migrated_engine, "SELECT count(*) FROM hr.attendance_day") == 0
    assert world.storage.objects == {}
    assert (
        _count(
            migrated_engine,
            "SELECT count(*) FROM hr.audit_log WHERE action = 'HOUSEKEEPING_PURGE'"
            " AND changes->'counts'->>'days' = '1'",
        )
        == 1
    )


def test_a_file_that_cannot_be_removed_is_reported_not_hidden(world, migrated_engine):
    keeper = _keeper(world)
    emp, user = _person(world)
    assert send(world, user, "in", token(world, user), where=NEAR).status_code == 200
    world.storage.fail = True
    done = world.client.post(
        URL + "/purge",
        json={**_scope("ATTENDANCE", "2026-11-16", "2026-11-16"), "confirm": "DELETE"},
        headers=world.headers(keeper),
    )
    assert done.status_code == 200 and done.json()["filesNotRemoved"] == 1
    assert _count(migrated_engine, "SELECT count(*) FROM hr.attendance_day") == 0


def test_leave_purge_removes_requests_and_their_ledger_rows_only(world, migrated_engine):
    keeper = _keeper(world)
    emp, _ = _person(world)
    _leave(migrated_engine, emp["employeeId"], "2026-10-07")
    _leave(migrated_engine, emp["employeeId"], "2026-11-03")
    r = world.client.post(
        URL + "/purge",
        json={**_scope("LEAVE", "2026-10-01", "2026-10-06"), "confirm": "DELETE"},
        headers=world.headers(keeper),
    )
    assert r.status_code == 200 and r.json()["counts"]["requests"] == 0
    r = world.client.post(
        URL + "/purge",
        json={**_scope("LEAVE", "2026-10-07", "2026-10-07"), "confirm": "DELETE"},
        headers=world.headers(keeper),
    )
    assert r.status_code == 200
    assert r.json()["counts"] == {"requests": 1, "ledgerRows": 1}
    assert _count(migrated_engine, "SELECT count(*) FROM hr.leave_request") == 1
    assert _count(migrated_engine, "SELECT count(*) FROM hr.leave_ledger") == 1


def test_the_ledger_is_still_append_only_outside_a_purge(world, migrated_engine):
    emp, _ = _person(world)
    _leave(migrated_engine, emp["employeeId"], "2026-10-07")
    with pytest.raises(DBAPIError):
        with migrated_engine.begin() as conn:
            conn.execute(text("DELETE FROM hr.leave_ledger"))
    with pytest.raises(DBAPIError):
        with migrated_engine.begin() as conn:
            conn.execute(text("UPDATE hr.leave_ledger SET note = 'x'"))
    assert _count(migrated_engine, "SELECT count(*) FROM hr.leave_ledger") == 1


def test_claims_in_a_payroll_are_kept_and_the_rest_go_with_receipts_and_files(
    world, migrated_engine
):
    keeper = _keeper(world)
    emp, _ = _person(world)
    gone = _claim(migrated_engine, emp["employeeId"], "2026-10-05")
    kept = _claim(migrated_engine, emp["employeeId"], "2026-10-05", status="PAID")
    world.storage.objects[f"claims/{gone}/r.jpg"] = (b"x", "image/jpeg")
    world.storage.objects[f"claims/{kept}/r.jpg"] = (b"x", "image/jpeg")
    body = _scope("CLAIMS", "2026-10-01", "2026-10-31")
    seen = world.client.post(URL + "/preview", json=body, headers=world.headers(keeper)).json()
    assert seen["counts"] == {"claims": 1, "receipts": 1, "keptInPayroll": 1}
    done = world.client.post(
        URL + "/purge", json={**body, "confirm": "DELETE"}, headers=world.headers(keeper)
    )
    assert done.status_code == 200, done.text
    with migrated_engine.connect() as conn:
        left = [str(r[0]) for r in conn.execute(text("SELECT claim_id FROM hr.claim"))]
    assert left == [kept]
    assert _count(migrated_engine, "SELECT count(*) FROM hr.claim_receipt") == 1
    assert _count(migrated_engine, "SELECT count(*) FROM hr.claim_event") == 1
    assert list(world.storage.objects) == [f"claims/{kept}/r.jpg"]


def test_a_month_a_payroll_already_used_cannot_be_cleared(world, migrated_engine):
    keeper = _keeper(world)
    emp, user = _person(world)
    assert send(world, user, "in", token(world, user), where=NEAR).status_code == 200
    _leave(migrated_engine, emp["employeeId"], "2026-10-07")
    _run(migrated_engine, "2026-10-01", "APPROVED")
    _run(migrated_engine, "2026-11-01", "PAID")
    for kind, a, b, month in (
        ("ATTENDANCE", "2026-11-01", "2026-11-16", "Nov 2026"),
        ("LEAVE", "2026-10-01", "2026-10-31", "Oct 2026"),
    ):
        scope = _scope(kind, a, b)
        seen = world.client.post(URL + "/preview", json=scope, headers=world.headers(keeper)).json()
        assert month in seen["blockedBy"]
        r = world.client.post(
            URL + "/purge", json={**scope, "confirm": "DELETE"}, headers=world.headers(keeper)
        )
        assert r.status_code == 409 and r.json()["code"] == "HOUSEKEEPING_PAYROLL_USED"
    assert _count(migrated_engine, "SELECT count(*) FROM hr.attendance_day") == 1
    assert _count(migrated_engine, "SELECT count(*) FROM hr.leave_request") == 1


def test_a_draft_payroll_does_not_block_and_dates_are_checked(world, migrated_engine):
    keeper = _keeper(world)
    _run(migrated_engine, "2026-10-01", "DRAFT")
    ok = world.client.post(
        URL + "/preview",
        json=_scope("ATTENDANCE", "2026-10-01", "2026-10-31"),
        headers=world.headers(keeper),
    )
    assert ok.status_code == 200 and ok.json()["blockedBy"] is None
    for a, b in (
        ("2026-11-16", "2026-11-01"),  # backwards
        ("2026-11-10", "2026-11-20"),  # reaches the future
        ("2026-10-01", "2026-11-01"),  # 32 days
    ):
        bad = world.client.post(
            URL + "/preview", json=_scope("LEAVE", a, b), headers=world.headers(keeper)
        )
        assert bad.status_code == 422, (a, b)
