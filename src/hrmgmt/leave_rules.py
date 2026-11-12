"""Leave arithmetic. All days are counted on the server: Sundays and declared holidays are not
leave days, a half day is 0.5, and a request stays inside one calendar year. Holidays are the
person's own (hrmgmt.holidays): on a half-day holiday only half the day is a working day, so a
full day's leave across it counts 0.5, and half-day leave on it is refused (7 Nov 2026).

Paid leave is earned, not granted (product owner, 4 Nov 2026). Completing a block of attendance
days (`leave.accrual_attendance_days`, 22) earns `leave.accrual_paid_days` (1) paid leave, up to a
cap on the balance (`leave.paid_balance_cap`, 3); what the cap stops is recorded as lapsed. A day
of attendance is a working day with a check-in: it counts 1, or 0.5 when a late, early or
out-of-fence exception on it was rejected. A day whose exception is still waiting stops the count
there until it is decided, so the blocks never change once written. Days worked on a Sunday or
holiday earn a comp-off day instead (once approved), not attendance days; work on the off half of
a half-day holiday earns half a comp-off day. A half-day holiday whose working half was attended
counts as half an attendance day (7 Nov 2026): halves add up as halves towards the 22. A Sunday
made a working day for the person counts like any working day.

Special occasion leave is birthday leave (product owner, 7 Nov 2026). Each birthday opens
`leave.special_days_per_year` days, usable from the birthday up to the day before the next one; it
never carries over and cannot be taken before the birthday that opens it. Its ledger rows carry the
year of that birthday in leave_year (a 29 Feb birthday is 28 Feb in other years)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

from hrmgmt import holidays as hol
from hrmgmt import settings_store as cfg
from hrmgmt.attendance_correction import VALUES, apply_to_units
from hrmgmt.errors import ApiError

# Leave paid from a balance. PAID and COMP_OFF balances carry over the years; SPECIAL is a quota
# per calendar year. SICK and EARNED exist only in history (the yearly allowance before Nov 2026).
PAID = ("PAID", "SPECIAL", "COMP_OFF")
YEARLY = ("SPECIAL",)
LEGACY = ("SICK", "EARNED")
MAX_RANGE_DAYS = 60
_ZERO = Decimal(0)


def leave_days(
    conn: Connection, start: date, end: date, half_day: bool, employee_id: str | None
) -> Decimal:
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
    holidays = hol.holidays_for(conn, employee_id, start, end)
    count = sum((hol.working_fraction(day, holidays.get(day)) for day in _days(start, end)), _ZERO)
    if count == 0:
        raise ApiError(422, "LEAVE_NO_WORKING_DAYS", "There are no working days in these dates.")
    if half_day:
        half = hol.half_day(start, holidays.get(start))
        if half is not None:
            off = "morning" if half.day_part == hol.MORNING else "afternoon"
            raise ApiError(
                422,
                "LEAVE_HALF_DAY_ON_HALF_HOLIDAY",
                f"{start:%d %b %Y} is already a half-day holiday ({half.name}, {off} off), so only"
                " the other half is a working day. Apply for a full day instead: it counts as half"
                " a day of leave.",
            )
        return Decimal("0.5")
    return count


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


# ---- birthday leave (SPECIAL) -----------------------------------------------------------------

BIRTHDAY = "Birthday"
# Note prefix of the quota correction entries written by migration 0027.
QUOTA_CORRECTION = "Quota correction"
NO_DATE_OF_BIRTH = "Your date of birth is not in myHR; ask HR to add it."


def birthday_in(dob: date, year: int) -> date:
    """The birthday in a given year; 29 Feb falls on 28 Feb in a year that has none."""
    try:
        return date(year, dob.month, dob.day)
    except ValueError:
        return date(year, 2, 28)


def birthday_window(dob: date, day: date) -> tuple[int, date, date]:
    """(year of the birthday that opened it, first day, last day) of the birthday leave a date
    falls in: from a birthday to the day before the next one."""
    year = day.year if day >= birthday_in(dob, day.year) else day.year - 1
    return year, birthday_in(dob, year), birthday_in(dob, year + 1) - timedelta(days=1)


def first_open_year(dob: date, joined: date | None) -> int | None:
    """The year of the first birthday on or after joining: earlier birthdays open nothing for this
    person (product owner, 7 Nov 2026). None: no joining date, every birthday counts."""
    if joined is None:
        return None
    return joined.year if birthday_in(dob, joined.year) >= joined else joined.year + 1


def window_exists(dob: date, joined: date | None, year: int) -> bool:
    first = first_open_year(dob, joined)
    return first is None or year >= first


def date_of_joining(conn: Connection, employee_id: str) -> date | None:
    value = conn.execute(
        text("SELECT date_of_joining FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"),
        {"e": employee_id},
    ).scalar()
    return value if isinstance(value, date) else None


def date_of_birth(conn: Connection, employee_id: str) -> date | None:
    value = conn.execute(
        text("SELECT date_of_birth FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"),
        {"e": employee_id},
    ).scalar()
    return value if isinstance(value, date) else None


def _special_refused(code: str, detail: str) -> ApiError:
    return ApiError(422, code, detail)


def special_window(conn: Connection, employee_id: str, start: date, end: date) -> int:
    """The birthday year whose leave these dates use. Refused with a plain message when there is
    no date of birth, or when the dates run across a birthday (the next leave opens on it)."""
    dob = date_of_birth(conn, employee_id)
    if dob is None:
        raise _special_refused("LEAVE_NO_DATE_OF_BIRTH", NO_DATE_OF_BIRTH)
    year, _, last = birthday_window(dob, start)
    joined = date_of_joining(conn, employee_id)
    if not window_exists(dob, joined, year):
        first = first_open_year(dob, joined)
        assert first is not None
        raise _special_refused(
            "LEAVE_BIRTHDAY_NOT_OPEN",
            f"Birthday leave opens on your birthday, {birthday_in(dob, first):%d %b %Y}, the first"
            " birthday after you joined.",
        )
    if end > last:
        opens = last + timedelta(days=1)
        raise _special_refused(
            "LEAVE_BIRTHDAY_NOT_OPEN",
            f"Birthday leave opens on your birthday, {opens:%d %b %Y}. These dates run across it:"
            " apply for the days before it and from it separately.",
        )
    return year


def special_used_message(dob: date, year: int) -> str:
    _, opened, last = birthday_window(dob, birthday_in(dob, year))
    return (
        f"Your birthday leave for the birthday of {opened:%d %b %Y} has been used or is waiting for"
        f" approval. Birthday leave opens on your birthday, {last + timedelta(days=1):%d %b %Y}."
    )


def ensure_grants(conn: Connection, employee_id: str, year: int) -> None:
    """The birthday leave of the birthday in `year` is granted the first time it is needed (no
    grant without a date of birth). A unique index makes a second grant impossible even if two
    requests arrive together."""
    days = Decimal(str(cfg.load_all(conn)["leave.special_days_per_year"]))
    dob = date_of_birth(conn, employee_id)
    if days <= 0 or dob is None:
        return
    if not window_exists(dob, date_of_joining(conn, employee_id), year):
        return  # a birthday before joining opens nothing
    conn.execute(
        text(
            "INSERT INTO hr.leave_ledger (employee_id, leave_type, leave_year, entry_type, days, note)"
            " VALUES (CAST(:e AS uuid), 'SPECIAL', :y, 'GRANT', :d, :n)"
            " ON CONFLICT DO NOTHING"
        ),
        {
            "e": employee_id,
            "y": year,
            "d": days,
            "n": f"Birthday leave for the birthday of {birthday_in(dob, year):%d %b %Y}",
        },
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
    # An approved attendance correction says what its day counts, with or without a check-in.
    corrected = {
        r[0]: VALUES[r[1]]
        for r in conn.execute(
            text(
                "SELECT work_date, requested FROM hr.attendance_correction"
                " WHERE employee_id = CAST(:e AS uuid) AND status = 'APPROVED'"
                " AND requested IN ('FULL_DAY', 'HALF_DAY')"  # a check-out correction is a check-out
                " AND (CAST(:a AS date) IS NULL OR work_date > :a) AND work_date < :b"
            ),
            {"e": employee_id, "a": after, "b": before},
        )
    }
    if not rows and not corrected:
        return []
    days = [r[0] for r in rows] + list(corrected)
    holidays = hol.holidays_for(conn, employee_id, min(days), max(days))
    out: list[tuple[date, Decimal | None]] = []
    for day, waiting, rejected in rows:
        share = hol.working_fraction(day, holidays.get(day))
        if share == 0:
            continue  # off-day work earns a comp-off, not an attendance day
        # Plain arithmetic (product owner, 7 Nov 2026): a day counts at most its working share, so
        # an attended half-day holiday is 0.5, and halves add up as halves towards the 22.
        if waiting:
            out.append((day, None))
        else:
            out.append((day, min(Decimal("0.5") if rejected else Decimal(1), share)))
    capped = {
        d: min(v, hol.working_fraction(d, holidays.get(d)))
        for d, v in corrected.items()
        if hol.working_fraction(d, holidays.get(d)) > 0
    }
    return apply_to_units(out, capped)


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
            SELECT x.exception_id, x.work_date, coalesce(x.comp_off_days, 1)
            FROM hr.attendance_exception x
            WHERE x.employee_id = CAST(:e AS uuid) AND x.kind = 'OFF_DAY_WORK'
              AND x.status = 'APPROVED' AND x.decided_at >= :s
              AND NOT EXISTS (SELECT 1 FROM hr.leave_ledger l
                              WHERE l.exception_id = x.exception_id AND l.entry_type = 'EARN')
            ORDER BY x.work_date
            """
        ),
        {"e": employee_id, "s": start},
    ).all()
    for exception_id, work_date, days in rows:
        half = Decimal(days) < 1
        conn.execute(
            text(
                "INSERT INTO hr.leave_ledger (employee_id, leave_type, leave_year, entry_type, days,"
                " note, exception_id) VALUES (CAST(:e AS uuid), 'COMP_OFF', :y, 'EARN', :d, :n,"
                " CAST(:x AS uuid)) ON CONFLICT DO NOTHING"
            ),
            {
                "e": employee_id,
                "y": work_date.year,
                "d": Decimal(days),
                "n": (
                    f"Half a comp-off for working the holiday half on {work_date.isoformat()}"
                    if half
                    else f"Comp-off for working on {work_date.isoformat()}"
                ),
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


def special_year_for(conn: Connection, employee_id: str, day: date) -> int | None:
    """The birthday year a date's birthday leave belongs to (None: no date of birth, or that
    birthday came before joining, so there is no birthday leave for that date)."""
    dob = date_of_birth(conn, employee_id)
    if dob is None:
        return None
    year = birthday_window(dob, day)[0]
    return year if window_exists(dob, date_of_joining(conn, employee_id), year) else None


def balances(
    conn: Connection,
    employee_id: str,
    year: int,
    today: date | None = None,
    *,
    special_on: date | None = None,
) -> dict[str, dict[str, Decimal]]:
    """PAID and COMP_OFF: everything earned and used so far. SPECIAL: the birthday leave of the
    birthday window holding `special_on` (else today; else the birthday year `year`). No date of
    birth: nothing."""
    if today is not None:
        accrue(conn, employee_id, today)
    on = special_on or today
    special = special_year_for(conn, employee_id, on) if on is not None else year
    if special is not None:
        ensure_grants(conn, employee_id, special)
    dob = date_of_birth(conn, employee_id)
    window = birthday_window(dob, birthday_in(dob, special)) if dob and special else None
    year = special if special is not None else -1
    out: dict[str, dict[str, Decimal]] = {
        t: {"granted": _ZERO, "used": _ZERO, "pending": _ZERO, "balance": _ZERO} for t in PAID
    }
    for r in conn.execute(
        text(
            "SELECT leave_type, entry_type, sum(days) FROM hr.leave_ledger"
            " WHERE employee_id = CAST(:e AS uuid) AND leave_type IN ('PAID', 'COMP_OFF')"
            " GROUP BY leave_type, entry_type"
            " UNION ALL"
            # A quota correction (migration 0027) lowers what was granted; it is not leave used.
            " SELECT leave_type, entry_type, sum(days) FROM ("
            "   SELECT leave_type, days, CASE WHEN entry_type = 'ADJUST' AND note LIKE :qc"
            "   THEN 'GRANT' ELSE entry_type END AS entry_type FROM hr.leave_ledger"
            "   WHERE employee_id = CAST(:e AS uuid) AND leave_type = 'SPECIAL' AND leave_year = :y"
            " ) s GROUP BY leave_type, entry_type"
        ),
        {"e": employee_id, "y": year, "qc": QUOTA_CORRECTION + "%"},
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
            " AND status = 'PENDING' AND leave_type = 'SPECIAL' AND from_date >= :a AND from_date <= :b"
            " GROUP BY leave_type"
        ),
        {
            "e": employee_id,
            "y": year,
            "a": window[1] if window else date.max,
            "b": window[2] if window else date.min,
        },
    ):
        out[r[0]]["pending"] = Decimal(r[1])
    return out


# ---- leave taken recently (product owner, 7 Nov 2026; 4 weeks since 12 Nov 2026) -------------

# 12 Nov 2026: "when approval goes, it should have record how many leaves a person has took in
# last 4 weeks" (was 6 weeks). The employee applying sees the same figure as the approver.
RECENT_WINDOW_DAYS = 28
# Paid: from a balance, legacy SICK and EARNED included. Unpaid: UNPAID.
PAID_TAKEN = ("PAID", "SPECIAL", "COMP_OFF", "SICK", "EARNED")


def recent_leave(conn: Connection, employee_id: str, before: date, today: date) -> dict[str, Any]:
    """Approved paid and unpaid leave days already taken in the 4 weeks (28 days) ending the day
    before `before` (a request's from date) or yesterday, whichever is earlier, so approved future
    leave is not counted as taken. Counted like any leave day: Sundays and the person's holidays
    left out, a half day 0.5, a day on a half-day holiday at most 0.5. Only days inside count."""
    end = min(before, today) - timedelta(days=1)
    start = end - timedelta(days=RECENT_WINDOW_DAYS - 1)
    holidays = hol.holidays_for(conn, employee_id, start, end)
    paid = unpaid = _ZERO
    for leave_type, first, last, half in conn.execute(
        text(
            "SELECT leave_type, from_date, to_date, half_day FROM hr.leave_request"
            " WHERE employee_id = CAST(:e AS uuid) AND status = 'APPROVED'"
            " AND from_date <= :b AND to_date >= :a"
        ),
        {"e": employee_id, "a": start, "b": end},
    ):
        for day in _days(max(first, start), min(last, end)):
            amount = min(
                Decimal("0.5") if half else Decimal(1), hol.working_fraction(day, holidays.get(day))
            )
            if leave_type in PAID_TAKEN:
                paid += amount
            else:
                unpaid += amount
    return {
        "windowFrom": start.isoformat(),
        "windowTo": end.isoformat(),
        "paidDays": float(paid),
        "unpaidDays": float(unpaid),
    }


def special_info(conn: Connection, employee_id: str, today: date) -> dict[str, Any]:
    """For the balance cards: the birthday leave window holding today, and when the next opens."""
    dob = date_of_birth(conn, employee_id)
    if dob is None:
        return {"dateOfBirthOnFile": False, "message": NO_DATE_OF_BIRTH}
    year, first, last = birthday_window(dob, today)
    opens = first_open_year(dob, date_of_joining(conn, employee_id))
    if opens is not None and year < opens:
        # Joined after this window's birthday: nothing until the first birthday after joining.
        first = birthday_in(dob, opens)
        return {
            "dateOfBirthOnFile": True,
            "open": False,
            "birthdayYear": opens,
            "availableFrom": first.isoformat(),
            "availableUntil": birthday_window(dob, first)[2].isoformat(),
            "nextOpensOn": first.isoformat(),
        }
    return {
        "dateOfBirthOnFile": True,
        "open": True,
        "birthdayYear": year,
        "availableFrom": first.isoformat(),
        "availableUntil": last.isoformat(),
        "nextOpensOn": (last + timedelta(days=1)).isoformat(),
    }
