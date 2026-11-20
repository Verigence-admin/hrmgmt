"""The night's sleep (product owner, 13 Nov 2026): the system sleeps from 12:30 am to 8:00 am IST.

The database is billed for every hour it runs and stops by itself only after five minutes in
which nothing asked it anything. myHR's background jobs therefore never touch it on their own
clock inside this window: a job that falls due at night waits for the end of it. A request from a
person is served as always.

`VERIGENCE_SLEEP_WINDOW_IST` sets the window ("00:30-08:00"); "off" means never sleep. It is the
same setting Audit Core and Security read."""

from __future__ import annotations

import os
from datetime import datetime, time, timedelta

from hrmgmt.timeutil import to_ist, utc_now

DEFAULT_WINDOW = "00:30-08:00"

Window = tuple[time, time]


def parse_window(value: str | None) -> Window | None:
    """`"00:30-08:00"` as two IST times; None for "off". Anything unreadable is the default
    window: a typing mistake must neither keep the database up all night nor put jobs to sleep
    by day."""
    raw = (value or "").strip().lower()
    if raw in {"off", "none", "never", "false", "0"}:
        return None
    for candidate in (raw, DEFAULT_WINDOW):
        start_text, _, end_text = candidate.partition("-")
        try:
            start = time.fromisoformat(start_text.strip())
            end = time.fromisoformat(end_text.strip())
        except ValueError:
            continue
        if start != end:
            return start, end
    return None


WINDOW: Window | None = parse_window(os.environ.get("VERIGENCE_SLEEP_WINDOW_IST", DEFAULT_WINDOW))


def in_window(moment: datetime | None = None) -> bool:
    if WINDOW is None:
        return False
    start, end = WINDOW
    local = to_ist(moment or utc_now()).time()
    return start <= local < end if start < end else (local >= start or local < end)


def seconds_until_wake(moment: datetime | None = None) -> float:
    """Seconds from `moment` to the end of the window it is in; 0 outside the window."""
    if WINDOW is None or not in_window(moment):
        return 0.0
    end = WINDOW[1]
    local = to_ist(moment or utc_now())
    wake = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if wake <= local:
        wake += timedelta(days=1)
    return (wake - local).total_seconds()


def outside_sleep(wait_s: float, now: datetime | None = None) -> float:
    """A wait of `wait_s` seconds from `now`, stretched to the end of the window when it would
    otherwise end inside it."""
    now = now or utc_now()
    return wait_s + seconds_until_wake(now + timedelta(seconds=wait_s))
