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
no check-out, or a rejected exception). One pending or approved correction per day.

A MISSED CHECK-OUT (product owner, 10 Nov 2026): someone who checked in but could not check out --
today or an earlier day of this month -- asks for the check-out to be recorded at the time they
left (CHECK_OUT, with that time). Once the Team Lead or Project Manager approves it, the check-out
is written to the day at that time, flagged as recorded from an approved correction (no photo, no
location), and the day counts like any other from then on. It does not say what the day counts, so
it is never one of the approved values below.

A TIME OR LOCATION CORRECTION (product owner, 16 Nov 2026): someone who checked in asks for their
check-in time to be earlier, their check-out time to be later (or a missing one recorded), or their
check-in or check-out location to count as at their outlet (ADJUST, any of these together). Once the
Team Lead or Project Manager approves it, the times are written to the day (flagged as from an
approved correction; what was recorded before is kept on the correction), and a late, early,
out-of-fence or short-day exception the correction answers becomes RESOLVED. Like a check-out, it
does not say what the day counts.

A day has at most one waiting correction, and at most one approved full or half day; approved time
corrections may follow one another."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

FULL_DAY = "FULL_DAY"
HALF_DAY = "HALF_DAY"
CHECK_OUT = "CHECK_OUT"
ADJUST = "ADJUST"
# The kinds that change the day's record rather than what it counts.
RECORD_KINDS = (CHECK_OUT, ADJUST)
VALUES = {FULL_DAY: Decimal(1), HALF_DAY: Decimal("0.5")}
# The kinds that say what a day counts (CHECK_OUT records the check-out instead).
DAY_COUNT_KINDS = (FULL_DAY, HALF_DAY)
# The flag on a check-in or check-out written from an approved correction.
CORRECTED_CHECK_OUT_FLAG = "APPROVED_CORRECTION"
CORRECTED_FLAG = CORRECTED_CHECK_OUT_FLAG
# The flag on a side whose location an approved correction says was at the outlet.
LOCATION_CORRECTED_FLAG = "LOCATION_CORRECTED"


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
                " AND requested = ANY(:k) AND work_date >= :a AND work_date <= :b"
            ),
            {"e": employee_id, "a": first, "b": last, "k": list(DAY_COUNT_KINDS)},
        )
    }


def live_by_day(
    conn: Connection, first: date, last: date
) -> dict[tuple[str, date], dict[str, Any]]:
    """(employee, day) -> the pending or approved correction, for the attendance views. A waiting
    one is shown before an approved one; an approved full or half day before a time correction."""
    out: dict[tuple[str, date], dict[str, Any]] = {}
    for r in conn.execute(
        text(
            "SELECT correction_id, employee_id, work_date, requested, reason, status, check_out_at,"
            " check_in_at, location_check_in, location_check_out"
            " FROM hr.attendance_correction WHERE status IN ('PENDING', 'APPROVED')"
            " AND work_date >= :a AND work_date <= :b"
            " ORDER BY (status = 'PENDING'), (requested IN ('FULL_DAY', 'HALF_DAY')), created_at"
        ),
        {"a": first, "b": last},
    ).mappings():
        out[(str(r["employee_id"]), r["work_date"])] = {
            "correctionId": str(r["correction_id"]),
            "requested": r["requested"],
            "reason": r["reason"],
            "status": r["status"],
            "checkOutAt": r["check_out_at"].isoformat() if r["check_out_at"] else None,
            "checkInAt": r["check_in_at"].isoformat() if r["check_in_at"] else None,
            "locationCheckIn": r["location_check_in"],
            "locationCheckOut": r["location_check_out"],
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
