"""Company time is Indian Standard Time. The server clock decides; a phone's clock never does."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_ist(moment: datetime) -> datetime:
    return moment.astimezone(IST)


def ist_date(moment: datetime) -> date:
    return to_ist(moment).date()


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def is_sunday(day: date) -> bool:
    return day.weekday() == 6
