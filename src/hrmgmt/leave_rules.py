"""Leave arithmetic. All days are counted on the server: Sundays and declared holidays are not
leave days, a half day is 0.5, and a request stays inside one calendar year."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import Connection, text

from hrmgmt import settings_store as cfg
from hrmgmt.errors import ApiError
from hrmgmt.timeutil import is_sunday

PAID = ("SICK", "EARNED")
MAX_RANGE_DAYS = 60


def leave_days(conn: Connection, start: date, end: date, half_day: bool) -> Decimal:
    if end < start:
        raise ApiError(422, "LEAVE_DATES_INVALID", "The end date is before the start date.")
    if start.year != end.year:
        raise ApiError(
            422,
            "LEAVE_DATES_INVALID",
            "A leave request stays within one calendar year. Apply separately for each.",
        )
    if (end - start).days > MAX_RANGE_DAYS:
        raise ApiError(
            422, "LEAVE_DATES_INVALID", f"Apply for at most {MAX_RANGE_DAYS} days at a time."
        )
    if half_day and start != end:
        raise ApiError(422, "LEAVE_DATES_INVALID", "A half day is for a single date.")
    holidays = {
        r[0]
        for r in conn.execute(
            text(
                "SELECT holiday_date FROM hr.holiday WHERE status = 'DECLARED' AND holiday_date BETWEEN :a AND :b"
            ),
            {"a": start, "b": end},
        )
    }
    count = 0
    day = start
    while day <= end:
        if not is_sunday(day) and day not in holidays:
            count += 1
        day += timedelta(days=1)
    if count == 0:
        raise ApiError(422, "LEAVE_NO_WORKING_DAYS", "There are no working days in these dates.")
    return Decimal("0.5") if half_day else Decimal(count)


def ensure_grants(conn: Connection, employee_id: str, year: int) -> None:
    """The year's allowance is granted the first time it is needed. A unique index makes a
    second grant impossible even if two requests arrive together."""
    values = cfg.load_all(conn)
    for leave_type, key in (
        ("SICK", "leave.sick_days_per_year"),
        ("EARNED", "leave.earned_days_per_year"),
    ):
        days = Decimal(str(values[key]))
        if days <= 0:
            continue
        conn.execute(
            text(
                "INSERT INTO hr.leave_ledger (employee_id, leave_type, leave_year, entry_type, days, note)"
                " VALUES (CAST(:e AS uuid), :t, :y, 'GRANT', :d, 'Yearly allowance')"
                " ON CONFLICT DO NOTHING"
            ),
            {"e": employee_id, "t": leave_type, "y": year, "d": days},
        )


def balances(conn: Connection, employee_id: str, year: int) -> dict[str, dict[str, Decimal]]:
    ensure_grants(conn, employee_id, year)
    out: dict[str, dict[str, Decimal]] = {
        t: {"granted": Decimal(0), "used": Decimal(0), "pending": Decimal(0), "balance": Decimal(0)}
        for t in PAID
    }
    for r in conn.execute(
        text(
            "SELECT leave_type, entry_type, sum(days) FROM hr.leave_ledger"
            " WHERE employee_id = CAST(:e AS uuid) AND leave_year = :y GROUP BY leave_type, entry_type"
        ),
        {"e": employee_id, "y": year},
    ):
        bucket = out[r[0]]
        total = Decimal(r[2])
        bucket["balance"] += total
        if r[1] in ("GRANT", "ADJUST") and total > 0:
            bucket["granted"] += total
        elif r[1] in ("DEDUCT", "REVERSAL", "ADJUST"):
            bucket["used"] += -total
    for r in conn.execute(
        text(
            "SELECT leave_type, sum(days) FROM hr.leave_request WHERE employee_id = CAST(:e AS uuid)"
            " AND status = 'PENDING' AND EXTRACT(year FROM from_date) = :y GROUP BY leave_type"
        ),
        {"e": employee_id, "y": year},
    ):
        if r[0] in out:
            out[r[0]]["pending"] = Decimal(r[1])
    return out
