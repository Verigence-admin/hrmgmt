"""Attendance correction requests (product owner, 6 Nov 2026).

An employee asks for one past working day to count as a FULL_DAY or a HALF_DAY: they forgot to
check in or out, the app failed them, or an exception on the day was rejected. It needs a reason
and goes to whoever decides the person's attendance (a Team Lead or Project Manager of their
project, HR when there is none), waits in the same place and counts for the Monday 09:00 deadline.

An approved correction is what the day counts, everywhere: payroll (present days, and no half-day
loss for that day) and earned leave. Nothing else about the day is rewritten: the check-in, the
check-out, the photos and the exceptions stay as they were, so the history shows what happened and
what was agreed.

What can be corrected: a past working day (not a Sunday, a declared holiday or a day of approved
full-day leave), on or after joining, in the current calendar month (product owner 7 Nov 2026), before that month's
payroll has gone past draft, and only when the day does not already count in full (no check-in,
no check-out, or a rejected exception). One pending or approved correction per day."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

FULL_DAY = "FULL_DAY"
HALF_DAY = "HALF_DAY"
VALUES = {FULL_DAY: Decimal(1), HALF_DAY: Decimal("0.5")}


def approved_values(
    conn: Connection, employee_id: str, first: date, last: date
) -> dict[date, Decimal]:
    """Day -> what an approved correction says it counts (1 or 0.5), inclusive range."""
    return {
        r[0]: VALUES[r[1]]
        for r in conn.execute(
            text(
                "SELECT work_date, requested FROM hr.attendance_correction"
                " WHERE employee_id = CAST(:e AS uuid) AND status = 'APPROVED'"
                " AND work_date >= :a AND work_date <= :b"
            ),
            {"e": employee_id, "a": first, "b": last},
        )
    }


def live_by_day(
    conn: Connection, first: date, last: date
) -> dict[tuple[str, date], dict[str, Any]]:
    """(employee, day) -> the pending or approved correction, for the attendance views."""
    out: dict[tuple[str, date], dict[str, Any]] = {}
    for r in conn.execute(
        text(
            "SELECT correction_id, employee_id, work_date, requested, reason, status"
            " FROM hr.attendance_correction WHERE status IN ('PENDING', 'APPROVED')"
            " AND work_date >= :a AND work_date <= :b"
        ),
        {"a": first, "b": last},
    ).mappings():
        out[(str(r["employee_id"]), r["work_date"])] = {
            "correctionId": str(r["correction_id"]),
            "requested": r["requested"],
            "reason": r["reason"],
            "status": r["status"],
        }
    return out


def apply_to_units(
    units: list[tuple[date, Decimal | None]], corrected: dict[date, Decimal]
) -> list[tuple[date, Decimal | None]]:
    """Earned leave: an approved correction replaces what a day counted, or adds a day that had no
    check-in. The result stays in date order."""
    if not corrected:
        return units
    merged: dict[date, Decimal | None] = dict(units)
    merged.update(corrected)
    return sorted(merged.items())
