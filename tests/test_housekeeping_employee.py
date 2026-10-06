"""SuperAdmin deletes an employee for good (people added only to test the system)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from hrmgmt import permissions as perm
from tests.support import World, ist

URL = "/hr/v1/housekeeping/employee"


@pytest.fixture()
def world(migrated_engine):
    return World(migrated_engine, now=ist(2026, 11, 16, 10, 0))


def keeper(world):
    return world.grant(str(uuid.uuid4()), perm.HR_HOUSEKEEPING_MANAGE)


def person(world):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE, perm.HR_EMPLOYEE_READ)
    emp, user = world.employee(hr)
    return emp, user


def post(world, who, path, **body):
    return world.client.post(f"{URL}/{path}", json=body, headers=world.headers(who))


def count(world, table, emp):
    with world.engine.connect() as conn:
        return conn.execute(
            text(f"SELECT count(*) FROM hr.{table} WHERE employee_id = CAST(:e AS uuid)"),
            {"e": emp["employeeId"]},
        ).scalar_one()


def run(world, sql, **params):
    with world.engine.begin() as conn:
        result = conn.execute(text(sql), params)
        return result.scalar() if result.returns_rows else None


def fill(world, emp):
    """Leaves records of every kind behind for this employee; returns the stored file keys."""
    e = emp["employeeId"]
    run(
        world,
        "INSERT INTO hr.attendance_day (employee_id, work_date, check_in_photo_key)"
        " VALUES (CAST(:e AS uuid), '2026-10-05', :k)",
        e=e,
        k=f"attendance/{e}/in.jpg",
    )
    request_id = run(
        world,
        "INSERT INTO hr.leave_request (employee_id, leave_type, from_date, to_date, days, status,"
        " approver_rule) VALUES (CAST(:e AS uuid), 'SICK', '2026-10-06', '2026-10-06', 1,"
        " 'APPROVED', 'HR') RETURNING request_id",
        e=e,
    )
    run(
        world,
        "INSERT INTO hr.leave_ledger (employee_id, leave_type, leave_year, entry_type, days,"
        " request_id) VALUES (CAST(:e AS uuid), 'SICK', 2026, 'DEDUCT', -1, :r)",
        e=e,
        r=request_id,
    )
    claim_id = run(
        world,
        "INSERT INTO hr.claim (employee_id, category_code, expense_date, amount, status,"
        " payroll_month) VALUES (CAST(:e AS uuid), 'BIKE_TAXI', '2026-10-07', 100, 'SUBMITTED',"
        " '2026-10-01') RETURNING claim_id",
        e=e,
    )
    run(
        world,
        "INSERT INTO hr.claim_receipt (claim_id, file_key, content_type, size_bytes)"
        " VALUES (:c, :k, 'image/jpeg', 3)",
        c=claim_id,
        k=f"claims/{claim_id}/r.jpg",
    )
    run(
        world,
        "INSERT INTO hr.salary_structure (employee_id, gross_monthly, components, effective_from,"
        " proposed_by) VALUES (CAST(:e AS uuid), 30000, '[]'::jsonb, '2026-10-01', 'test')"
        " RETURNING 1",
        e=e,
    )
    ticket_id = run(
        world,
        "INSERT INTO hr.ticket (employee_id, employee_code, employee_name, employee_email, summary)"
        " VALUES (CAST(:e AS uuid), 'X', 'X', 'x@y.com', 'help') RETURNING ticket_id",
        e=e,
    )
    message_id = run(
        world,
        "INSERT INTO hr.ticket_message (ticket_id, author_user_id, author_kind, author_name, body)"
        " VALUES (:t, 'u', 'EMPLOYEE', 'X', 'hi') RETURNING message_id",
        t=ticket_id,
    )
    run(
        world,
        "INSERT INTO hr.ticket_file (ticket_id, message_id, file_key, file_name, content_type,"
        " size_bytes) VALUES (:t, :m, :k, 'a.png', 'image/png', 3) RETURNING 1",
        t=ticket_id,
        m=message_id,
        k=f"tickets/{ticket_id}/a.png",
    )
    run(
        world,
        "UPDATE hr.employee SET photo_updated_at = now() WHERE employee_id = CAST(:e AS uuid)"
        " RETURNING 1",
        e=e,
    )
    keys = [f"attendance/{e}/in.jpg", f"claims/{claim_id}/r.jpg", f"tickets/{ticket_id}/a.png"]
    keys.append(f"employee/{e}/photo.jpg")
    for key in keys:
        world.storage.objects[key] = (b"x", "image/jpeg")
    return keys


def test_only_the_housekeeping_permission_may_preview_or_delete(world):
    emp, _ = person(world)
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE, perm.HR_SETTINGS_MANAGE)
    assert post(world, hr, "preview", employee_code=emp["employeeCode"]).status_code == 403
    assert (
        post(world, hr, "delete", employee_code=emp["employeeCode"], confirm="DELETE").status_code
        == 403
    )


def test_the_preview_shows_what_would_go_and_changes_nothing(world):
    emp, _ = person(world)
    fill(world, emp)
    r = post(world, keeper(world), "preview", employee_code=emp["employeeCode"].lower())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["employeeCode"] == emp["employeeCode"] and body["blockedBy"] is None
    assert body["counts"]["attendanceDays"] == 1 and body["counts"]["claims"] == 1
    assert body["counts"]["leaveRequests"] == 1 and body["counts"]["leaveLedgerRows"] == 1
    assert body["counts"]["salaryStructures"] == 1 and body["counts"]["supportTickets"] == 1
    assert body["hasLogin"] is True and "Users" in body["note"]
    assert count(world, "attendance_day", emp) == 1 and count(world, "claim", emp) == 1


def test_the_delete_removes_the_person_and_everything_of_theirs_and_their_files(world):
    emp, _ = person(world)
    other, _ = person(world)
    keys = fill(world, emp)
    fill(world, other)
    who = keeper(world)
    assert post(world, who, "delete", employee_code=emp["employeeCode"]).status_code == 422
    assert (
        post(world, who, "delete", employee_code=emp["employeeCode"], confirm="delete").status_code
        == 422
    )
    r = post(world, who, "delete", employee_code=emp["employeeCode"], confirm="DELETE")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True and r.json()["filesNotRemoved"] == 0
    for table in (
        "attendance_day",
        "leave_request",
        "leave_ledger",
        "claim",
        "salary_structure",
        "ticket",
        "employee_status_change",
    ):
        assert count(world, table, emp) == 0, table
    with world.engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"),
                {"e": emp["employeeId"]},
            ).scalar_one()
            == 0
        )
    assert all(k not in world.storage.objects for k in keys)
    # nobody else was touched
    assert count(world, "attendance_day", other) == 1 and count(world, "claim", other) == 1
    # the history stays and says who deleted whom (no protected numbers)
    with world.engine.connect() as conn:
        audit = conn.execute(
            text(
                "SELECT actor_user_id, changes::text FROM hr.audit_log WHERE entity_id = :e"
                " AND action = 'EMPLOYEE_HARD_DELETED'"
            ),
            {"e": emp["employeeId"]},
        ).one()
    assert audit[0] == who and emp["employeeCode"] in audit[1]
    assert "ABCDE1234F" not in audit[1] and "123456789012" not in audit[1]
    # it is gone: a second delete finds nobody
    assert (
        post(world, who, "delete", employee_code=emp["employeeCode"], confirm="DELETE").status_code
        == 404
    )


def test_an_employee_in_a_payroll_is_never_deleted(world):
    emp, _ = person(world)
    e = emp["employeeId"]
    structure = run(
        world,
        "INSERT INTO hr.salary_structure (employee_id, gross_monthly, components, effective_from,"
        " proposed_by, status) VALUES (CAST(:e AS uuid), 30000, '[]'::jsonb, '2026-10-01', 'test',"
        " 'APPROVED') RETURNING structure_id",
        e=e,
    )
    month = f"{2030 + uuid.uuid4().int % 60}-01-01"
    run_id = run(
        world,
        "INSERT INTO hr.payroll_run (pay_month, status, statutory_config, created_by)"
        " VALUES (:m, 'DRAFT', '{}'::jsonb, 'test') RETURNING run_id",
        m=month,
    )
    run(
        world,
        "INSERT INTO hr.payroll_line (run_id, employee_id, structure_id, employee_code, employee_name,"
        " figures, net_pay, payable_total) VALUES (:r, CAST(:e AS uuid), :s, 'X', 'X',"
        " '{}'::jsonb, 1, 1) RETURNING 1",
        r=run_id,
        e=e,
        s=structure,
    )
    who = keeper(world)
    preview = post(world, who, "preview", employee_code=emp["employeeCode"]).json()
    assert preview["blockedBy"] and "payroll" in preview["blockedBy"]
    r = post(world, who, "delete", employee_code=emp["employeeCode"], confirm="DELETE")
    assert r.status_code == 409 and r.json()["code"] == "HOUSEKEEPING_EMPLOYEE_IN_PAYROLL"
    assert count(world, "salary_structure", emp) == 1
    run(world, "DELETE FROM hr.payroll_line WHERE run_id = :r RETURNING 1", r=run_id)
    run(world, "DELETE FROM hr.payroll_run WHERE run_id = :r RETURNING 1", r=run_id)


def test_an_employee_with_a_paid_reimbursement_is_never_deleted(world):
    emp, _ = person(world)
    run(
        world,
        "INSERT INTO hr.claim (employee_id, category_code, expense_date, amount, status,"
        " payroll_month) VALUES (CAST(:e AS uuid), 'BIKE_TAXI', '2026-10-07', 100, 'PAID',"
        " '2026-10-01') RETURNING 1",
        e=emp["employeeId"],
    )
    r = post(world, keeper(world), "delete", employee_code=emp["employeeCode"], confirm="DELETE")
    assert r.status_code == 409 and count(world, "claim", emp) == 1


def test_an_unknown_code_is_not_found(world):
    r = post(world, keeper(world), "preview", employee_code="NOPE999")
    assert r.status_code == 404


def test_the_pending_status_request_goes_with_the_employee(world):
    emp, _ = person(world)
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    r = world.client.post(
        f"/hr/v1/employees/{emp['employeeId']}/status-change",
        json={"to_status": "QUIT", "reason": "Test"},
        headers=world.headers(hr),
    )
    assert r.status_code == 201
    done = post(world, keeper(world), "delete", employee_code=emp["employeeCode"], confirm="DELETE")
    assert done.status_code == 200 and done.json()["counts"]["statusChanges"] == 1
