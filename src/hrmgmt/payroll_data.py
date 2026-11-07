"""Reads what a payroll month needs from the rest of HR: days worked, leave, structures and the
reimbursements waiting to be paid. One place, so a run and its tests agree."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

from hrmgmt import attendance_grace as grace
from hrmgmt import holidays as hol
from hrmgmt import payroll_calc as pcalc
from hrmgmt.attendance_correction import approved_values
from hrmgmt.claim_rules import add_months


def month_end(month: date) -> date:
    return add_months(month, 1) - timedelta(days=1)


Holidays = dict[date, hol.DayHoliday]


def declared_holidays(conn: Connection, month: date) -> dict[str, Holidays]:
    """employee -> the declared holidays of the month that apply to them (hrmgmt.holidays), for
    every active employee. One read for the whole run."""
    ids = [
        str(r[0])
        for r in conn.execute(
            text("SELECT employee_id FROM hr.employee WHERE employment_status = 'ACTIVE'")
        )
    ]
    return hol.holidays_by_employee(conn, ids, month, month_end(month))


def holidays_of(conn: Connection, employee_id: str, month: date) -> Holidays:
    return hol.holidays_for(conn, employee_id, month, month_end(month))


def working_days(month: date, holidays: Holidays) -> Decimal:
    """Days of the month that are working days: a half-day holiday counts 0.5."""
    day, count = month, Decimal(0)
    while day <= month_end(month):
        count += hol.working_fraction(day, holidays.get(day))
        day += timedelta(days=1)
    return count


def leave_days_in_month(
    conn: Connection, employee_id: str, month: date, holidays: Holidays
) -> tuple[Decimal, Decimal]:
    """Approved paid and unpaid leave days that fall in the month (Sundays and holidays excluded;
    on a half-day holiday a day of leave counts at most 0.5)."""
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
            share = hol.working_fraction(day, holidays.get(day))
            if share > 0:
                amount = min(Decimal("0.5") if half else Decimal(1), share)
                if leave_type == "UNPAID":
                    unpaid += amount
                else:
                    paid += amount
            day += timedelta(days=1)
    return paid, unpaid


def present_days(
    conn: Connection, employee_id: str, month: date, holidays: Holidays | None = None
) -> Decimal:
    """Days with a check-in, except where an approved attendance correction says what the day
    counts (1 or 0.5); a corrected day with no check-in counts too. On a half-day holiday the day
    counts at most 0.5, the working half, so present days never exceed working days."""
    corrected = approved_values(conn, employee_id, month, month_end(month))
    checked_in = {
        r[0]
        for r in conn.execute(
            text(
                "SELECT work_date FROM hr.attendance_day WHERE employee_id = CAST(:e AS uuid)"
                " AND check_in_at IS NOT NULL AND work_date BETWEEN :a AND :b"
            ),
            {"e": employee_id, "a": month, "b": month_end(month)},
        )
    }
    holidays = holidays or {}

    def cap(day: date, value: Decimal) -> Decimal:
        h = holidays.get(day)
        return min(value, Decimal("0.5")) if hol.half_day(day, h) is not None else value

    return sum((cap(d, Decimal(1)) for d in checked_in - corrected.keys()), Decimal(0)) + sum(
        (cap(d, v) for d, v in corrected.items()), Decimal(0)
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
    holidays: Holidays,
) -> tuple[Any, dict[str, Any]]:
    """`holidays`: the declared holidays of the month that apply to this employee."""
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
        present_days=present_days(conn, str(employee["employee_id"]), month, holidays),
        paid_leave_days=paid_leave,
        unpaid_leave_days=unpaid_leave,
        extra_lop_days=extra_lop,
        before_joining_days=before,
        half_day_lop=grace.half_days(conn, str(employee["employee_id"]), month),
        late_early_beyond_grace=grace.beyond_grace(conn, str(employee["employee_id"]), month),
    )
    figures = pcalc.compute_line(
        structure=list(structure["components"]),
        days=days,
        statutory=statutory,
        claims=unpaid_claims(conn, str(employee["employee_id"]), month),
        adjustments=adjustments,
        pf_applicable=bool(structure["pf_applicable"]),
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
    by_employee = declared_holidays(conn, month)
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
            holidays = by_employee.get(eid)
            if holidays is None:
                holidays = holidays_of(conn, eid, month)
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
