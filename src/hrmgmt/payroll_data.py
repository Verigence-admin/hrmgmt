"""Reads what a payroll month needs from the rest of HR: days worked, leave, structures and the
reimbursements waiting to be paid. One place, so a run and its tests agree."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

from hrmgmt import payroll_calc as pcalc
from hrmgmt.claim_rules import add_months
from hrmgmt.timeutil import is_sunday


def month_end(month: date) -> date:
    return add_months(month, 1) - timedelta(days=1)


def declared_holidays(conn: Connection, month: date) -> set[date]:
    return {
        r[0]
        for r in conn.execute(
            text(
                "SELECT holiday_date FROM hr.holiday WHERE status = 'DECLARED' AND holiday_date BETWEEN :a AND :b"
            ),
            {"a": month, "b": month_end(month)},
        )
    }


def working_days(month: date, holidays: set[date]) -> int:
    day, count = month, 0
    while day <= month_end(month):
        if not is_sunday(day) and day not in holidays:
            count += 1
        day += timedelta(days=1)
    return count


def leave_days_in_month(
    conn: Connection, employee_id: str, month: date, holidays: set[date]
) -> tuple[Decimal, Decimal]:
    """Approved paid and unpaid leave days that fall in the month (Sundays and holidays excluded)."""
    paid = Decimal(0)
    unpaid = Decimal(0)
    end = month_end(month)
    rows = conn.execute(
        text(
            "SELECT leave_type, from_date, to_date, half_day FROM hr.leave_request"
            " WHERE employee_id = CAST(:e AS uuid) AND status = 'APPROVED' AND from_date <= :b AND to_date >= :a"
        ),
        {"e": employee_id, "a": month, "b": end},
    )
    for leave_type, start, finish, half in rows:
        day = max(start, month)
        while day <= min(finish, end):
            if not is_sunday(day) and day not in holidays:
                amount = Decimal("0.5") if half else Decimal(1)
                if leave_type == "UNPAID":
                    unpaid += amount
                else:
                    paid += amount
            day += timedelta(days=1)
    return paid, unpaid


def present_days(conn: Connection, employee_id: str, month: date) -> Decimal:
    return Decimal(
        conn.execute(
            text(
                "SELECT count(*) FROM hr.attendance_day WHERE employee_id = CAST(:e AS uuid)"
                " AND check_in_at IS NOT NULL AND work_date BETWEEN :a AND :b"
            ),
            {"e": employee_id, "a": month, "b": month_end(month)},
        ).scalar_one()
    )


def structure_for(conn: Connection, employee_id: str, month: date) -> Any:
    return (
        conn.execute(
            text(
                "SELECT * FROM hr.salary_structure WHERE employee_id = CAST(:e AS uuid) AND status = 'APPROVED'"
                " AND effective_from <= :end ORDER BY effective_from DESC, proposed_at DESC LIMIT 1"
            ),
            {"e": employee_id, "end": month_end(month)},
        )
        .mappings()
        .first()
    )


def unpaid_claims(conn: Connection, employee_id: str, month: date) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT c.claim_id, c.amount, c.expense_date, k.label, k.taxable FROM hr.claim c"
            " JOIN hr.claim_category k ON k.category_code = c.category_code"
            " WHERE c.employee_id = CAST(:e AS uuid) AND c.status = 'APPROVED' AND c.payroll_run_id IS NULL"
            " AND c.payroll_month <= :m ORDER BY c.expense_date, c.claim_id"
        ),
        {"e": employee_id, "m": month},
    ).mappings()
    return [
        {
            "claim_id": str(r["claim_id"]),
            "amount": str(r["amount"]),
            "date": r["expense_date"].isoformat(),
            "label": r["label"],
            "taxable": r["taxable"],
        }
        for r in rows
    ]


def build_line(
    conn: Connection,
    employee: Any,
    month: date,
    statutory: dict[str, Any],
    extra_lop: Decimal,
    adjustments: list[dict[str, Any]],
    holidays: set[date],
) -> tuple[Any, dict[str, Any]]:
    """Returns (the salary structure used, the worked-out figures). Raises PayrollError."""
    structure = structure_for(conn, str(employee["employee_id"]), month)
    if structure is None:
        raise pcalc.PayrollError("No approved salary structure.")
    paid_leave, unpaid_leave = leave_days_in_month(
        conn, str(employee["employee_id"]), month, holidays
    )
    joined = employee["date_of_joining"]
    before = (
        (joined.day - 1)
        if joined and joined.year == month.year and joined.month == month.month
        else 0
    )
    days = pcalc.Days(
        in_month=pcalc.days_in_month(month),
        working_days=working_days(month, holidays),
        present_days=present_days(conn, str(employee["employee_id"]), month),
        paid_leave_days=paid_leave,
        unpaid_leave_days=unpaid_leave,
        extra_lop_days=extra_lop,
        before_joining_days=before,
    )
    figures = pcalc.compute_line(
        structure=list(structure["components"]),
        days=days,
        statutory=statutory,
        claims=unpaid_claims(conn, str(employee["employee_id"]), month),
        adjustments=adjustments,
    )
    return structure, figures


def prepare_lines(
    conn: Connection,
    run_id: str,
    month: date,
    statutory: dict[str, Any],
    keep: dict[str, tuple[Decimal, list[dict[str, Any]]]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """(Re)builds every line of a draft run. HR's own inputs (extra loss of pay, adjustments) are
    kept. Returns (people left out with the reason, number of lines written)."""
    keep = keep or {}
    holidays = declared_holidays(conn, month)
    employees = (
        conn.execute(
            text(
                "SELECT e.employee_id, e.employee_code, e.full_name, e.date_of_joining, d.label AS designation"
                " FROM hr.employee e LEFT JOIN hr.designation d ON d.code = e.designation_code"
                " WHERE e.employment_status = 'ACTIVE' AND (e.date_of_joining IS NULL OR e.date_of_joining <= :end)"
                " ORDER BY e.employee_code"
            ),
            {"end": month_end(month)},
        )
        .mappings()
        .all()
    )
    conn.execute(text("DELETE FROM hr.payroll_line WHERE run_id = CAST(:r AS uuid)"), {"r": run_id})
    skipped: list[dict[str, Any]] = []
    written = 0
    for e in employees:
        eid = str(e["employee_id"])
        extra, adjustments = keep.get(eid, (Decimal(0), []))
        try:
            structure, figures = build_line(conn, e, month, statutory, extra, adjustments, holidays)
        except pcalc.PayrollError as exc:
            skipped.append(
                {
                    "employeeId": eid,
                    "employeeCode": e["employee_code"],
                    "employeeName": e["full_name"],
                    "reason": str(exc),
                }
            )
            continue
        conn.execute(
            text(
                "INSERT INTO hr.payroll_line (run_id, employee_id, structure_id, employee_code, employee_name,"
                " designation, extra_lop_days, adjustments, figures, net_pay, payable_total)"
                " VALUES (CAST(:r AS uuid), CAST(:e AS uuid), :s, :c, :n, :d, :x, CAST(:adj AS jsonb), CAST(:f AS jsonb), :net, :pay)"
            ),
            {
                "r": run_id,
                "e": eid,
                "s": structure["structure_id"],
                "c": e["employee_code"],
                "n": e["full_name"],
                "d": e["designation"],
                "x": extra,
                "adj": json.dumps(adjustments),
                "f": json.dumps(figures),
                "net": figures["net_pay"],
                "pay": figures["payable_total"],
            },
        )
        written += 1
    return skipped, written
