"""Company settings HR may change. The defaults here are the agreed starting values; a row in
hr.setting overrides one. Every key has a type and a range, so a typo cannot break attendance."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

from hrmgmt.errors import ApiError


@dataclass(frozen=True)
class Spec:
    kind: str  # "time", "int", "money" or "money_or_null"
    default: Any
    low: float | None = None
    high: float | None = None
    label: str = ""
    group: str = ""


SPECS: dict[str, Spec] = {
    "attendance.check_in_standard": Spec(
        "time", "10:30", label="Standard check-in time", group="Attendance"
    ),
    "attendance.late_after": Spec("time", "11:15", label="Late check-in after", group="Attendance"),
    "attendance.check_out_earliest": Spec(
        "time", "17:00", label="Earliest check-out", group="Attendance"
    ),
    "attendance.check_out_standard": Spec(
        "time", "18:30", label="Standard check-out time", group="Attendance"
    ),
    "attendance.geofence_radius_m": Spec(
        "int", 500, 50, 5000, "Geofence radius for PCs (metres)", "Attendance"
    ),
    "attendance.max_accuracy_m": Spec(
        "int", 100, 10, 1000, "Worst location accuracy accepted (metres)", "Attendance"
    ),
    "attendance.max_position_age_s": Spec(
        "int", 60, 5, 600, "Oldest location fix accepted (seconds)", "Attendance"
    ),
    "attendance.capture_token_ttl_s": Spec(
        "int", 120, 30, 600, "Time allowed to take the photo (seconds)", "Attendance"
    ),
    "leave.sick_days_per_year": Spec("int", 5, 0, 60, "Sick leave days per year", "Leave"),
    "leave.earned_days_per_year": Spec("int", 5, 0, 60, "Earned leave days per year", "Leave"),
    "claims.travel_monthly_limit": Spec(
        "money", 5000, 0, 10_000_000, "Travel limit per employee per month (₹)", "Reimbursement"
    ),
    "claims.finance_threshold": Spec(
        "money",
        3000,
        0,
        10_000_000,
        "Travel total above which Finance approves (₹)",
        "Reimbursement",
    ),
    "claims.meals_monthly_limit": Spec(
        "money_or_null",
        None,
        0,
        10_000_000,
        "Meals limit per employee per month (₹, blank = none set)",
        "Reimbursement",
    ),
    "claims.cutoff_day": Spec(
        "int",
        25,
        1,
        28,
        "Claims submitted by this day go into that month's payroll",
        "Reimbursement",
    ),
    "claims.stale_after_months": Spec(
        "int", 2, 1, 24, "Claims older than this many months need Finance approval", "Reimbursement"
    ),
    "claims.personal_bike_rate_per_km": Spec(
        "money", 0, 0, 1000, "Personal bike rate per km (₹)", "Reimbursement"
    ),
}

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def validate(key: str, value: Any) -> Any:
    spec = SPECS.get(key)
    if spec is None:
        raise ApiError(422, "HR_SETTING_UNKNOWN", f"Unknown setting {key}.")
    if spec.kind == "time":
        if not isinstance(value, str) or not _TIME.match(value):
            raise ApiError(422, "HR_SETTING_INVALID", f"{spec.label}: use a time like 10:30.")
        return value
    if spec.kind == "money_or_null" and value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ApiError(422, "HR_SETTING_INVALID", f"{spec.label}: enter a number.")
    try:
        number = float(value)
    except ValueError as exc:
        raise ApiError(422, "HR_SETTING_INVALID", f"{spec.label}: enter a number.") from exc
    if spec.low is not None and number < spec.low or spec.high is not None and number > spec.high:
        raise ApiError(
            422,
            "HR_SETTING_INVALID",
            f"{spec.label}: use a value from {spec.low:g} to {spec.high:g}.",
        )
    if spec.kind == "int":
        if number != int(number):
            raise ApiError(422, "HR_SETTING_INVALID", f"{spec.label}: use a whole number.")
        return int(number)
    return round(number, 2)


def load_all(conn: Connection) -> dict[str, Any]:
    values = {key: spec.default for key, spec in SPECS.items()}
    for row in conn.execute(text("SELECT key, value FROM hr.setting")):
        if row[0] in SPECS:
            values[row[0]] = row[1]
    return values


def get(conn: Connection, key: str) -> Any:
    row = conn.execute(text("SELECT value FROM hr.setting WHERE key = :k"), {"k": key}).first()
    return row[0] if row else SPECS[key].default
