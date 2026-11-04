"""Grace for late check-ins and early check-outs (product owner, 5 Nov 2026).

`attendance.grace_per_month` (1 to start) is how many late check-ins and early check-outs, counted
together, a person may have in a calendar month before they count in salary processing. Grace does
not replace the Team Lead: every late or early still goes for approval, and the Team Lead is shown
how much of the month's grace is used. Payroll shows how many went beyond grace; it deducts nothing
for them yet.

A rejected late, early or out-of-fence exception makes the day a half day (product owner,
4 Nov 2026), in attendance and in payroll."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

from hrmgmt import settings_store as cfg

LATE_EARLY = ("LATE_CHECK_IN", "EARLY_CHECK_OUT")
HALF_DAY_KINDS = ("LATE_CHECK_IN", "EARLY_CHECK_OUT", "OUT_OF_FENCE")


def month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return start, end


def grace_allowed(conn: Connection) -> int:
    return int(cfg.load_all(conn)["attendance.grace_per_month"])


def late_early_in_month(conn: Connection, employee_id: str, day: date) -> list[str]:
    """Every late check-in and early check-out of the month, oldest first, whatever its status."""
    start, end = month_bounds(day)
    return [
        str(r[0])
        for r in conn.execute(
            text(
                "SELECT exception_id FROM hr.attendance_exception"
                " WHERE employee_id = CAST(:e AS uuid) AND kind = ANY(:k)"
                " AND work_date >= :a AND work_date < :b ORDER BY work_date, created_at, exception_id"
            ),
            {"e": employee_id, "k": list(LATE_EARLY), "a": start, "b": end},
        )
    ]


def grace_view(conn: Connection, employee_id: str, day: date, exception_id: str) -> dict[str, Any]:
    allowed = grace_allowed(conn)
    month = late_early_in_month(conn, employee_id, day)
    position = month.index(exception_id) + 1 if exception_id in month else len(month)
    return {
        "allowed": allowed,
        "lateOrEarlyThisMonth": len(month),
        "thisIsNumber": position,
        "withinGrace": position <= allowed,
    }


def beyond_grace(conn: Connection, employee_id: str, month: date) -> int:
    return max(0, len(late_early_in_month(conn, employee_id, month)) - grace_allowed(conn))


def half_days(conn: Connection, employee_id: str, month: date) -> Decimal:
    """0.5 for every day of the month with a rejected late, early or out-of-fence exception."""
    start, end = month_bounds(month)
    count = conn.execute(
        text(
            "SELECT count(DISTINCT work_date) FROM hr.attendance_exception"
            " WHERE employee_id = CAST(:e AS uuid) AND status = 'REJECTED' AND kind = ANY(:k)"
            " AND work_date >= :a AND work_date < :b"
        ),
        {"e": employee_id, "k": list(HALF_DAY_KINDS), "a": start, "b": end},
    ).scalar_one()
    return Decimal(count) / 2
