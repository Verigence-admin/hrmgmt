"""Leave arithmetic. All days are counted on the server: Sundays and declared holidays are not
leave days, a half day is 0.5, and a request stays inside one calendar year.

Paid leave is earned, not granted (product owner, 4 Nov 2026). Completing a block of attendance
days (`leave.accrual_attendance_days`, 22) earns `leave.accrual_paid_days` (1) paid leave, up to a
cap on the balance (`leave.paid_balance_cap`, 3); what the cap stops is recorded as lapsed. A day
of attendance is a working day with a check-in: it counts 1, or 0.5 when a late, early or
out-of-fence exception on it was rejected. A day whose exception is still waiting stops the count
there until it is decided, so the blocks never change once written. Days worked on a Sunday or
holiday earn a comp-off day instead (once approved), not attendance days.

Special occasion leave has its own yearly quota (`leave.special_days_per_year`)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

from hrmgmt import settings_store as cfg
from hrmgmt.errors import ApiError
from hrmgmt.timeutil import is_sunday

# Leave paid from a balance. PAID and COMP_OFF balances carry over the years; SPECIAL is a quota
# per calendar year. SICK and EARNED exist only in history (the yearly allowance before Nov 2026).
PAID = ("PAID", "SPECIAL", "COMP_OFF")
YEARLY = ("SPECIAL",)
LEGACY = ("SICK", "EARNED")
MAX_RANGE_DAYS = 60
_ZERO = Decimal(0)


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
    holidays = declared_holidays(conn, start, end)
    count = sum(1 for day in _days(start, end) if not is_sunday(day) and day not in holidays)
    if count == 0:
        raise ApiError(422, "LEAVE_NO_WORKING_DAYS", "There are no working days in these dates.")
    return Decimal("0.5") if half_day else Decimal(count)


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def declared_holidays(conn: Connection, start: date, end: date) -> set[date]:
    return {
        r[0]
        for r in conn.execute(
            text(
                "SELECT holiday_date FROM hr.holiday WHERE status = 'DECLARED'"
                " AND holiday_date BETWEEN :a AND :b"
            ),
            {"a": start, "b": end},
        )
    }


def ensure_grants(conn: Connection, employee_id: str, year: int) -> None:
    """The year's Special occasion quota is granted the first time it is needed. A unique index
    makes a second grant impossible even if two requests arrive together."""
    days = Decimal(str(cfg.load_all(conn)["leave.special_days_per_year"]))
    if days <= 0:
        return
    conn.execute(
        text(
            "INSERT INTO hr.leave_ledger (employee_id, leave_type, leave_year, entry_type, days, note)"
            " VALUES (CAST(:e AS uuid), 'SPECIAL', :y, 'GRANT', :d, 'Yearly special occasion quota')"
            " ON CONFLICT DO NOTHING"
        ),
        {"e": employee_id, "y": year, "d": days},
    )


# ---- earning --------------------------------------------------------------------------------


def _ledger_total(conn: Connection, employee_id: str, leave_type: str) -> Decimal:
    return Decimal(
        conn.execute(
            text(
                "SELECT coalesce(sum(days), 0) FROM hr.leave_ledger"
                " WHERE employee_id = CAST(:e AS uuid) AND leave_type = :t"
            ),
            {"e": employee_id, "t": leave_type},
        ).scalar_one()
    )


def attendance_units(
    conn: Connection, employee_id: str, after: date | None, before: date
) -> list[tuple[date, Decimal | None]]:
    """Each working day with a check-in, after `after` and before `before`, in order, with what it
    counts: 1, 0.5 when an exception on it was rejected, None while one is still waiting."""
    rows = conn.execute(
        text(
            """
            SELECT d.work_date,
                   bool_or(x.status = 'PENDING') AS waiting,
                   bool_or(x.status = 'REJECTED') AS rejected
            FROM hr.attendance_day d
            LEFT JOIN hr.attendance_exception x
                   ON x.attendance_id = d.attendance_id AND x.kind <> 'OFF_DAY_WORK'
            WHERE d.employee_id = CAST(:e AS uuid) AND d.check_in_at IS NOT NULL
              AND (CAST(:a AS date) IS NULL OR d.work_date > :a) AND d.work_date < :b
            GROUP BY d.work_date ORDER BY d.work_date
            """
        ),
        {"e": employee_id, "a": after, "b": before},
    ).all()
    if not rows:
        return []
    holidays = declared_holidays(conn, rows[0][0], rows[-1][0])
    out: list[tuple[date, Decimal | None]] = []
    for day, waiting, rejected in rows:
        if is_sunday(day) or day in holidays:
            continue  # off-day work earns a comp-off, not an attendance day
        if waiting:
            out.append((day, None))
        else:
            out.append((day, Decimal("0.5") if rejected else Decimal(1)))
    return out


def _last_block(conn: Connection, employee_id: str) -> Any:
    return conn.execute(
        text(
            "SELECT block_no, through_date, carried FROM hr.leave_accrual"
            " WHERE employee_id = CAST(:e AS uuid) ORDER BY block_no DESC LIMIT 1"
        ),
        {"e": employee_id},
    ).first()


def accrue(conn: Connection, employee_id: str, today: date) -> None:
    """Writes every block of attendance completed before today, and the comp-off days earned.
    Safe to call any number of times: what is already written is never written again."""
    conn.execute(
        text("SELECT 1 FROM hr.employee WHERE employee_id = CAST(:e AS uuid) FOR UPDATE"),
        {"e": employee_id},
    )
    values = cfg.load_all(conn)
    need = Decimal(int(values["leave.accrual_attendance_days"]))
    earn = Decimal(int(values["leave.accrual_paid_days"]))
    cap = Decimal(int(values["leave.paid_balance_cap"]))

    last = _last_block(conn, employee_id)
    block_no = last[0] if last else 0
    count = Decimal(last[2]) if last else _ZERO
    balance = _ledger_total(conn, employee_id, "PAID")
    for day, units in attendance_units(conn, employee_id, last[1] if last else None, today):
        if units is None:
            break  # waits for the decision; later days are counted after it
        count += units
        if count < need:
            continue
        block_no += 1
        credited = max(_ZERO, min(earn, cap - balance))
        carried = count - need
        conn.execute(
            text(
                "INSERT INTO hr.leave_accrual (employee_id, block_no, through_date, present_days,"
                " credited, lapsed, carried) VALUES (CAST(:e AS uuid), :n, :d, :p, :c, :l, :k)"
            ),
            {
                "e": employee_id,
                "n": block_no,
                "d": day,
                "p": count,
                "c": credited,
                "l": earn - credited,
                "k": carried,
            },
        )
        if credited > 0:
            conn.execute(
                text(
                    "INSERT INTO hr.leave_ledger (employee_id, leave_type, leave_year, entry_type,"
                    " days, note) VALUES (CAST(:e AS uuid), 'PAID', :y, 'EARN', :d, :n)"
                ),
                {
                    "e": employee_id,
                    "y": day.year,
                    "d": credited,
                    "n": f"Earned for {need:g} days of attendance, completed {day.isoformat()}",
                },
            )
            balance += credited
        count = carried
    _credit_comp_offs(conn, employee_id)


def comp_off_from(conn: Connection) -> date | None:
    """Off-day work approved on or after this date earns a comp-off (set when the rule began)."""
    value = conn.execute(
        text("SELECT value #>> '{}' FROM hr.setting WHERE key = 'leave.comp_off_from'")
    ).scalar()
    return date.fromisoformat(value) if value else None


def _credit_comp_offs(conn: Connection, employee_id: str) -> None:
    start = comp_off_from(conn)
    if start is None:
        return
    rows = conn.execute(
        text(
            """
            SELECT x.exception_id, x.work_date FROM hr.attendance_exception x
            WHERE x.employee_id = CAST(:e AS uuid) AND x.kind = 'OFF_DAY_WORK'
              AND x.status = 'APPROVED' AND x.decided_at >= :s
              AND NOT EXISTS (SELECT 1 FROM hr.leave_ledger l
                              WHERE l.exception_id = x.exception_id AND l.entry_type = 'EARN')
            ORDER BY x.work_date
            """
        ),
        {"e": employee_id, "s": start},
    ).all()
    for exception_id, work_date in rows:
        conn.execute(
            text(
                "INSERT INTO hr.leave_ledger (employee_id, leave_type, leave_year, entry_type, days,"
                " note, exception_id) VALUES (CAST(:e AS uuid), 'COMP_OFF', :y, 'EARN', 1, :n,"
                " CAST(:x AS uuid)) ON CONFLICT DO NOTHING"
            ),
            {
                "e": employee_id,
                "y": work_date.year,
                "n": f"Comp-off for working on {work_date.isoformat()}",
                "x": str(exception_id),
            },
        )


def progress(conn: Connection, employee_id: str, today: date) -> dict[str, Any]:
    """How far the person is towards the next earned day, and every block so far."""
    values = cfg.load_all(conn)
    need = int(values["leave.accrual_attendance_days"])
    last = _last_block(conn, employee_id)
    count = Decimal(last[2]) if last else _ZERO
    waiting_from: date | None = None
    for day, units in attendance_units(conn, employee_id, last[1] if last else None, today):
        if units is None:
            waiting_from = day
            break
        count += units
    blocks = conn.execute(
        text(
            "SELECT block_no, through_date, present_days, credited, lapsed FROM hr.leave_accrual"
            " WHERE employee_id = CAST(:e AS uuid) ORDER BY block_no DESC LIMIT 24"
        ),
        {"e": employee_id},
    ).all()
    return {
        "attendanceDaysNeeded": need,
        "paidDaysEarned": int(values["leave.accrual_paid_days"]),
        "balanceCap": int(values["leave.paid_balance_cap"]),
        "attendanceDaysSoFar": float(count),
        "countPausedFrom": waiting_from.isoformat() if waiting_from else None,
        "blocks": [
            {
                "blockNo": b[0],
                "completedOn": b[1].isoformat(),
                "attendanceDays": float(b[2]),
                "credited": float(b[3]),
                "lapsed": float(b[4]),
            }
            for b in blocks
        ],
    }


# ---- balances -------------------------------------------------------------------------------


def balances(
    conn: Connection, employee_id: str, year: int, today: date | None = None
) -> dict[str, dict[str, Decimal]]:
    """PAID and COMP_OFF: everything earned and used so far. SPECIAL: the given year's quota."""
    if today is not None:
        accrue(conn, employee_id, today)
    ensure_grants(conn, employee_id, year)
    out: dict[str, dict[str, Decimal]] = {
        t: {"granted": _ZERO, "used": _ZERO, "pending": _ZERO, "balance": _ZERO} for t in PAID
    }
    for r in conn.execute(
        text(
            "SELECT leave_type, entry_type, sum(days) FROM hr.leave_ledger"
            " WHERE employee_id = CAST(:e AS uuid) AND leave_type IN ('PAID', 'COMP_OFF')"
            " GROUP BY leave_type, entry_type"
            " UNION ALL"
            " SELECT leave_type, entry_type, sum(days) FROM hr.leave_ledger"
            " WHERE employee_id = CAST(:e AS uuid) AND leave_type = 'SPECIAL' AND leave_year = :y"
            " GROUP BY leave_type, entry_type"
        ),
        {"e": employee_id, "y": year},
    ):
        bucket = out[r[0]]
        total = Decimal(r[2])
        bucket["balance"] += total
        if r[1] in ("GRANT", "EARN", "ADJUST") and total > 0:
            bucket["granted"] += total
        elif r[1] in ("DEDUCT", "REVERSAL", "ADJUST"):
            bucket["used"] += -total
    for r in conn.execute(
        text(
            "SELECT leave_type, sum(days) FROM hr.leave_request WHERE employee_id = CAST(:e AS uuid)"
            " AND status = 'PENDING' AND leave_type IN ('PAID', 'COMP_OFF')"
            " GROUP BY leave_type"
            " UNION ALL"
            " SELECT leave_type, sum(days) FROM hr.leave_request WHERE employee_id = CAST(:e AS uuid)"
            " AND status = 'PENDING' AND leave_type = 'SPECIAL' AND EXTRACT(year FROM from_date) = :y"
            " GROUP BY leave_type"
        ),
        {"e": employee_id, "y": year},
    ):
        out[r[0]]["pending"] = Decimal(r[1])
    return out
