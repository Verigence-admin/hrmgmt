"""Company holidays: full day or half day, for the whole company or for chosen projects and outlets
(product owner, 7 Nov 2026).

A holiday is a row of hr.holiday: a date, a name, a status (only DECLARED stops work; TENTATIVE is a
proposal shown with a caveat), a day part (FULL_DAY, or MORNING / AFTERNOON off) and who it applies
to: ALL (the whole company) or SELECTED, with scope rows in hr.holiday_scope (a whole project, or one
outlet of a project). A removed holiday keeps its row (removed_at), so history stays readable.

Which holiday applies to a person on a date is decided here and only here: DECLARED, not removed,
and either company-wide or matching one of the person's work assignments valid on that date (the
project, or the outlet). Someone with no assignment (head office) gets only company-wide holidays.
When several apply, a full day wins, and a morning off together with an afternoon off is a full day.

Every Sunday is a day off unless a WORKING_DAY entry applies to the person (kind WORKING_DAY: a
whole Sunday, declared, for the company or chosen projects/outlets; 7 Nov 2026). For those people
that Sunday is an ordinary working day in everything below. A holiday that also applies wins.

On a half-day holiday the other half is a working day. The times of that working half come from
`effective_values`: with the morning off the day starts at `attendance.half_day_split` (14:00) and a
late check-in is judged from there; with the afternoon off the day ends at the split, and no
check-in by the split is an absence. The minimum hours are halved."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

from hrmgmt.timeutil import IST, is_sunday, parse_hhmm, to_ist

FULL_DAY = "FULL_DAY"
MORNING = "MORNING"
AFTERNOON = "AFTERNOON"
DAY_PARTS = (FULL_DAY, MORNING, AFTERNOON)
# Kinds of entry: a holiday, or a Sunday made a working day.
HOLIDAY = "HOLIDAY"
WORKING_DAY = "WORKING_DAY"
DECLARED = "DECLARED"
TENTATIVE = "TENTATIVE"
ALL = "ALL"
SELECTED = "SELECTED"

_ONE = Decimal(1)
_HALF = Decimal("0.5")
_ZERO = Decimal(0)


@dataclass(frozen=True)
class DayHoliday:
    """What the holidays that apply to one person make of one date."""

    day_part: str
    names: tuple[str, ...]

    @property
    def name(self) -> str:
        return " / ".join(self.names)

    @property
    def is_full(self) -> bool:
        return self.day_part == FULL_DAY

    @property
    def is_half(self) -> bool:
        return self.day_part in (MORNING, AFTERNOON)

    @property
    def is_working_day(self) -> bool:
        """A Sunday made a working day (no holiday applies on top)."""
        return self.day_part == WORKING_DAY


def combine(parts: list[tuple[str, str]]) -> DayHoliday | None:
    """(day part, name) of every entry that applies -> what the day is. A full day wins; a morning
    off and an afternoon off together are a full day. A working-day entry (day part WORKING_DAY)
    counts only when no holiday applies."""
    working = [n for p, n in parts if p == WORKING_DAY]
    parts = [(p, n) for p, n in parts if p != WORKING_DAY]
    if not parts:
        return DayHoliday(WORKING_DAY, tuple(dict.fromkeys(working))) if working else None

    def names(selected: list[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(selected))

    full = [n for p, n in parts if p == FULL_DAY]
    if full:
        return DayHoliday(FULL_DAY, names(full))
    morning = [n for p, n in parts if p == MORNING]
    afternoon = [n for p, n in parts if p == AFTERNOON]
    if morning and afternoon:
        return DayHoliday(FULL_DAY, names(morning + afternoon))
    if morning:
        return DayHoliday(MORNING, names(morning))
    return DayHoliday(AFTERNOON, names(afternoon))


# ---- what a holiday means for a day ---------------------------------------------------------


def day_kind(day: date, holiday: DayHoliday | None) -> tuple[str, str | None]:
    """('WORKING', None), ('SUNDAY', None) or ('HOLIDAY', name). A half-day holiday is a working
    day (its working half is required); callers show the half separately. A working Sunday is
    WORKING (callers show it with working_sunday_view)."""
    if _sunday_off(day, holiday):
        return "SUNDAY", None
    if holiday is not None and holiday.is_full:
        return "HOLIDAY", holiday.name
    return "WORKING", None


def half_day(day: date, holiday: DayHoliday | None) -> DayHoliday | None:
    """The half-day holiday of a working day, if there is one."""
    if is_sunday(day) or holiday is None or not holiday.is_half:
        return None
    return holiday


def _sunday_off(day: date, holiday: DayHoliday | None) -> bool:
    return is_sunday(day) and not (holiday is not None and holiday.is_working_day)


def is_off_day(day: date, holiday: DayHoliday | None) -> bool:
    return _sunday_off(day, holiday) or (holiday is not None and holiday.is_full)


def working_fraction(day: date, holiday: DayHoliday | None) -> Decimal:
    """1 for a working day (a working Sunday too), 0.5 on a half-day holiday, 0 on a Sunday or a
    full holiday."""
    if is_off_day(day, holiday):
        return _ZERO
    return _HALF if holiday is not None and holiday.is_half else _ONE


def working_sunday_view(day: date, holiday: DayHoliday | None) -> dict[str, Any] | None:
    """{"name": ...} when this Sunday is a working day for the person, else None."""
    if not is_sunday(day) or holiday is None or not holiday.is_working_day:
        return None
    return {"name": holiday.name}


def half_day_view(holiday: DayHoliday | None, values: dict[str, Any]) -> dict[str, Any] | None:
    if holiday is None or not holiday.is_half:
        return None
    return {
        "name": holiday.name,
        "dayPart": holiday.day_part,
        "splitAt": values["attendance.half_day_split"],
    }


def half_day_words(holiday: DayHoliday, values: dict[str, Any]) -> str:
    """For example: Diwali eve, afternoon off from 14:00."""
    split = values["attendance.half_day_split"]
    if holiday.day_part == MORNING:
        return f"{holiday.name}, morning off until {split}"
    return f"{holiday.name}, afternoon off from {split}"


# ---- times of the working half ----------------------------------------------------------------


def _minutes(hhmm: str) -> int:
    t = parse_hhmm(hhmm)
    return t.hour * 60 + t.minute


def _hhmm(minutes: int) -> str:
    minutes = max(0, min(minutes, 23 * 60 + 59))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def effective_values(values: dict[str, Any], day_part: str | None) -> dict[str, Any]:
    """The attendance settings as they apply on a day with this half off (None: a normal day).

    MORNING off: the working half starts at the split; late after the split plus the usual allowance
    (late_after - check_in_standard, 45 minutes by default). Check-out as on a normal day.
    AFTERNOON off: check-in as normal; the earliest check-out without an early flag and the time
    after which a missing check-in is an absence are both the split.
    Either half: the minimum hours are halved."""
    if day_part not in (MORNING, AFTERNOON):
        return values
    out = dict(values)
    split = _minutes(values["attendance.half_day_split"])
    if day_part == MORNING:
        allowance = _minutes(values["attendance.late_after"]) - _minutes(
            values["attendance.check_in_standard"]
        )
        late = split + max(0, allowance)
        out["attendance.check_in_standard"] = _hhmm(split)
        out["attendance.late_after"] = _hhmm(late)
        out["attendance.absent_after"] = _hhmm(
            max(_minutes(values["attendance.absent_after"]), late)
        )
    else:
        out["attendance.check_out_earliest"] = _hhmm(split)
        out["attendance.check_out_standard"] = _hhmm(split)
        out["attendance.absent_after"] = _hhmm(split)
    out["attendance.min_work_hours"] = Decimal(str(values["attendance.min_work_hours"])) / 2
    return out


def standard_times_view(values: dict[str, Any]) -> dict[str, Any]:
    """The day's times as a person reads them (from effective_values for their own day): when the
    day starts, after when a check-in is late, the earliest check-out without an early flag, the
    standard end and when a missing check-in becomes an absence."""
    return {
        "checkIn": values["attendance.check_in_standard"],
        "lateAfter": values["attendance.late_after"],
        "checkOutEarliest": values["attendance.check_out_earliest"],
        "checkOut": values["attendance.check_out_standard"],
        "absentAfter": values["attendance.absent_after"],
        "minimumHours": float(values["attendance.min_work_hours"]),
    }


def _bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=IST)
    return start, start + timedelta(days=1)


def split_moment(day: date, values: dict[str, Any]) -> datetime:
    return datetime.combine(day, parse_hhmm(values["attendance.half_day_split"]), tzinfo=IST)


def halves(
    day: date, day_part: str, values: dict[str, Any]
) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
    """((off half), (working half)) as moments of the day, for a half-day holiday."""
    start, end = _bounds(day)
    split = split_moment(day, values)
    morning, afternoon = (start, split), (split, end)
    return (morning, afternoon) if day_part == MORNING else (afternoon, morning)


def overlap(a: datetime, b: datetime, window: tuple[datetime, datetime]) -> timedelta:
    lo, hi = max(a, window[0]), min(b, window[1])
    return max(hi - lo, timedelta(0))


def worked_in_halves(
    day: date, day_part: str, check_in: datetime, check_out: datetime, values: dict[str, Any]
) -> tuple[timedelta, timedelta]:
    """(time inside the off half, time inside the working half) between check-in and check-out."""
    off, working = halves(day, day_part, values)
    return overlap(check_in, check_out, off), overlap(check_in, check_out, working)


def off_half_worked(worked_off: timedelta, values: dict[str, Any]) -> bool:
    """Working the off half: at least half of the minimum hours inside it (2.5 h of 5)."""
    need = Decimal(str(values["attendance.min_work_hours"])) / 2
    return Decimal(int(worked_off.total_seconds())) >= need * 3600


def closed_for_check_in(at: datetime, day_part: str | None, values: dict[str, Any]) -> bool:
    """Past the time after which a missing check-in is an absence: the split when the afternoon
    is off, attendance.absent_after (17:00) otherwise."""
    limit = effective_values(values, day_part)["attendance.absent_after"]
    return to_ist(at).time() >= parse_hhmm(limit)


# ---- which holidays apply to whom ------------------------------------------------------------


@dataclass(frozen=True)
class _Row:
    holiday_id: str
    day: date
    name: str
    day_part: str
    applies_to: str
    scope: tuple[tuple[str, str | None], ...]
    kind: str = HOLIDAY


@dataclass(frozen=True)
class _Assignment:
    tenant_id: str
    outlet_id: str | None
    valid_from: datetime
    valid_to: datetime | None

    def valid_on(self, day: date) -> bool:
        start, end = _bounds(day)
        return self.valid_from < end and (self.valid_to is None or self.valid_to > start)


def _load(conn: Connection, first: date, last: date, status: str) -> list[_Row]:
    rows = (
        conn.execute(
            text(
                "SELECT holiday_id, holiday_date, name, day_part, applies_to, kind FROM hr.holiday"
                " WHERE removed_at IS NULL AND status = :s"
                " AND holiday_date >= :a AND holiday_date <= :b ORDER BY holiday_date, created_at"
            ),
            {"s": status, "a": first, "b": last},
        )
        .mappings()
        .all()
    )
    if not rows:
        return []
    scope: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    selected = [str(r["holiday_id"]) for r in rows if r["applies_to"] == SELECTED]
    if selected:
        for hid, tenant, outlet in conn.execute(
            text(
                "SELECT holiday_id, tenant_id, outlet_id FROM hr.holiday_scope"
                " WHERE holiday_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": selected},
        ):
            scope[str(hid)].append((str(tenant), str(outlet) if outlet else None))
    return [
        _Row(
            holiday_id=str(r["holiday_id"]),
            day=r["holiday_date"],
            name=str(r["name"]),
            day_part=str(r["day_part"]),
            applies_to=str(r["applies_to"]),
            scope=tuple(scope.get(str(r["holiday_id"]), [])),
            kind=str(r["kind"]),
        )
        for r in rows
    ]


def assignments_between(
    conn: Connection, user_ids: list[str], first: date, last: date
) -> dict[str, list[_Assignment]]:
    out: dict[str, list[_Assignment]] = defaultdict(list)
    if not user_ids:
        return out
    start, _ = _bounds(first)
    _, end = _bounds(last)
    for user, tenant, outlet, vfrom, vto in conn.execute(
        text(
            "SELECT security_user_id, tenant_id, outlet_id, valid_from, valid_to"
            " FROM hr.work_assignment WHERE security_user_id = ANY(CAST(:u AS uuid[]))"
            " AND valid_from < :end AND (valid_to IS NULL OR valid_to > :start)"
        ),
        {"u": user_ids, "start": start, "end": end},
    ):
        out[str(user)].append(_Assignment(str(tenant), str(outlet) if outlet else None, vfrom, vto))
    return out


def matches(scope: tuple[tuple[str, str | None], ...], assignments: list[_Assignment]) -> bool:
    """A scope row with no outlet is the whole project; one with an outlet is that outlet."""
    for tenant, outlet in scope:
        for a in assignments:
            if a.tenant_id != tenant:
                continue
            if outlet is None or a.outlet_id == outlet:
                return True
    return False


def applies(row: _Row, assignments: list[_Assignment]) -> bool:
    if row.applies_to == ALL:
        return True
    return matches(row.scope, [a for a in assignments if a.valid_on(row.day)])


def _resolve(rows: list[_Row], assignments: list[_Assignment]) -> dict[date, DayHoliday]:
    parts: dict[date, list[tuple[str, str]]] = defaultdict(list)
    for r in rows:
        if applies(r, assignments):
            parts[r.day].append((WORKING_DAY if r.kind == WORKING_DAY else r.day_part, r.name))
    out: dict[date, DayHoliday] = {}
    for day, found in parts.items():
        combined = combine(found)
        if combined is not None:
            out[day] = combined
    return out


def _user_ids(conn: Connection, employee_ids: list[str]) -> dict[str, str | None]:
    if not employee_ids:
        return {}
    return {
        str(e): (str(u) if u else None)
        for e, u in conn.execute(
            text(
                "SELECT employee_id, security_user_id FROM hr.employee"
                " WHERE employee_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": employee_ids},
        )
    }


def holidays_by_employee(
    conn: Connection,
    employee_ids: list[str],
    first: date,
    last: date,
    *,
    status: str = DECLARED,
) -> dict[str, dict[date, DayHoliday]]:
    """employee -> date -> the holiday that applies, for every date from first to last inclusive
    that has one. Two queries for the holidays and one for the assignments, whatever the count."""
    rows = _load(conn, first, last, status)
    if not rows:
        return {e: {} for e in employee_ids}
    users = _user_ids(conn, employee_ids)
    need_assignments = any(r.applies_to == SELECTED for r in rows)
    found = (
        assignments_between(conn, sorted({u for u in users.values() if u}), first, last)
        if need_assignments
        else {}
    )
    out: dict[str, dict[date, DayHoliday]] = {}
    for eid in employee_ids:
        user = users.get(eid)
        out[eid] = _resolve(rows, found.get(user, []) if user else [])
    return out


def holidays_for(
    conn: Connection,
    employee_id: str | None,
    first: date,
    last: date,
    *,
    status: str = DECLARED,
) -> dict[date, DayHoliday]:
    """The holidays that apply to one person (None: company-wide ones only), first to last inclusive."""
    if employee_id is None:
        return _resolve(_load(conn, first, last, status), [])
    return holidays_by_employee(conn, [employee_id], first, last, status=status)[employee_id]


def holiday_for(
    conn: Connection, employee_id: str | None, day: date, *, status: str = DECLARED
) -> DayHoliday | None:
    return holidays_for(conn, employee_id, day, day, status=status).get(day)


def declared_on(conn: Connection, day: date) -> list[dict[str, Any]]:
    """Every declared holiday on a date, with who it is for, for the HR and lead day views."""
    rows = conn.execute(
        text(
            "SELECT h.name, h.day_part, h.applies_to, h.kind,"
            " coalesce(array_agg(coalesce(s.outlet_name, s.project_name, s.tenant_id)"
            "   ORDER BY s.project_name, s.outlet_name) FILTER (WHERE s.scope_id IS NOT NULL),"
            "   '{}') AS scope"
            " FROM hr.holiday h LEFT JOIN hr.holiday_scope s ON s.holiday_id = h.holiday_id"
            " WHERE h.removed_at IS NULL AND h.status = 'DECLARED' AND h.holiday_date = :d"
            " GROUP BY h.holiday_id, h.name, h.day_part, h.applies_to, h.kind, h.created_at"
            " ORDER BY h.created_at"
        ),
        {"d": day},
    )
    return [
        {"name": n, "dayPart": p, "appliesTo": a, "kind": k, "scope": [str(x) for x in scope]}
        for n, p, a, k, scope in rows
    ]
