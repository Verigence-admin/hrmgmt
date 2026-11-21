"""The night's sleep (product owner, 13 Nov 2026): the system sleeps from 12:30 am to 8:00 am IST.

The database is billed for every hour it runs and stops by itself only after five minutes in
which nothing asked it anything. myHR's background jobs therefore never touch it on their own
clock inside this window: a job that falls due at night waits for the end of it. A request from a
person is served as always.

`VERIGENCE_SLEEP_WINDOW_IST` sets the window ("00:30-08:00"); "off" means never sleep. It is the
same setting Audit Core and Security read.

The shutdown switch (product owner, 21 Nov 2026: "lets have a tab in the verigence to have a full
shutdown of DB"). The window was a setting of each service. It is now state held by Verigence
Security, changed by a Super Admin or Executive from the System shutdown tab: the daily hours, a
"Shut down now" (shut to the next daily opening) and a "Start now". myHR asks Security for it once
a minute (`start_following`; Security answers from memory, no database) and `follow` takes the
answer. From then on "the window" here is that state, so a job that falls due during a shutdown
by hand waits for the end of it as it would at night. The answer carries the schedule, so myHR
keeps time by itself between answers and while Security cannot be reached; before the first
answer the setting above stands."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from hrmgmt.timeutil import to_ist, utc_now

logger = logging.getLogger(__name__)

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


SHUT = "SHUT"
OPEN = "OPEN"


@dataclass(frozen=True)
class Followed:
    """Security's shutdown switch as last heard: the daily hours (None: no daily shutdown) and a
    Shut down or Start by hand, in force until `until`."""

    hours: Window | None
    manual: str | None = None
    until: datetime | None = None


_followed: Followed | None = None
_followed_lock = threading.Lock()


def _when(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)).astimezone(UTC) if value else None
    except ValueError:
        return None


def follow(status: object) -> bool:
    """Take Security's answer (GET /security/v1/system/status) as the state to go by. False, and
    nothing changes, for an answer that is not one."""
    global _followed
    if not isinstance(status, dict) or "resting" not in status:
        return False
    hours: Window | None = None
    start_text, end_text = status.get("hoursFromIst"), status.get("hoursToIst")
    if start_text or end_text:
        try:
            start, end = time.fromisoformat(str(start_text)), time.fromisoformat(str(end_text))
        except ValueError:
            return False
        if start == end:
            return False
        hours = (start, end)
    manual = status.get("manual")
    until = _when(status.get("manualUntilUtc"))
    if manual not in {SHUT, OPEN} or until is None:
        manual, until = None, None
    with _followed_lock:
        _followed = Followed(hours=hours, manual=manual, until=until)
    return True


def unfollow() -> None:
    """Back to the setting alone (tests)."""
    global _followed
    with _followed_lock:
        _followed = None


def followed() -> Followed | None:
    with _followed_lock:
        return _followed


def _hours() -> Window | None:
    state = followed()
    return state.hours if state is not None else WINDOW


def _manual(moment: datetime) -> Followed | None:
    state = followed()
    if state is None or state.manual is None or state.until is None:
        return None
    return state if moment < state.until else None


def _in_hours(moment: datetime) -> bool:
    hours = _hours()
    if hours is None:
        return False
    start, end = hours
    local = to_ist(moment).time()
    return start <= local < end if start < end else (local >= start or local < end)


def _next_opening(moment: datetime) -> datetime | None:
    hours = _hours()
    if hours is None:
        return None
    local = to_ist(moment)
    wake = local.replace(hour=hours[1].hour, minute=hours[1].minute, second=0, microsecond=0)
    if wake <= local:
        wake += timedelta(days=1)
    return wake


def in_window(moment: datetime | None = None) -> bool:
    """Whether Verigence is shut at this moment: a Shut down or Start by hand while it lasts,
    else the daily hours."""
    at = moment or utc_now()
    manual = _manual(at)
    return manual.manual == SHUT if manual is not None else _in_hours(at)


def seconds_until_wake(moment: datetime | None = None) -> float:
    """Seconds from `moment` to the end of the shutdown it is in; 0 outside one."""
    at = moment or utc_now()
    if not in_window(at):
        return 0.0
    manual = _manual(at)
    wake = manual.until if manual is not None else _next_opening(at)
    if manual is not None and wake is not None and _in_hours(wake):
        # A shutdown by hand that runs out inside the daily hours goes on to that day's opening.
        wake = _next_opening(wake)
    return max(0.0, (wake - at).total_seconds()) if wake is not None else 0.0


# -- asking Security --------------------------------------------------------------------------
FOLLOW_EVERY_SECONDS = 60.0
_follower: threading.Thread | None = None
_follower_lock = threading.Lock()


def ask(security_base_url: str, *, timeout: float = 5.0) -> bool:
    """One question. True when Security's answer was taken; False otherwise, with the state left
    as it was: a failure is never a reason to sleep or to wake."""
    import httpx

    try:
        response = httpx.get(
            f"{security_base_url.rstrip('/')}/security/v1/system/status", timeout=timeout
        )
        if response.status_code != 200:
            return False
        return follow(response.json())
    except Exception:
        return False


def start_following(
    security_base_url: str, stop: threading.Event | None = None
) -> threading.Thread | None:
    """Ask once a minute, in a thread of its own, once per process. None when there is nowhere to
    ask, when VERIGENCE_SHUTDOWN_SWITCH=off, or when myHR was told never to sleep
    (VERIGENCE_SLEEP_WINDOW_IST=off)."""
    global _follower
    off = os.environ.get("VERIGENCE_SHUTDOWN_SWITCH", "on").strip().lower() in {
        "off",
        "false",
        "0",
        "no",
    }
    if off or not security_base_url.strip() or WINDOW is None:
        return None
    halt = stop or threading.Event()

    def run() -> None:
        heard: bool | None = None
        while True:
            answered = ask(security_base_url)
            if answered != heard:
                heard = answered
                logger.info(
                    "hr_shutdown_switch_heard" if answered else "hr_shutdown_switch_not_heard"
                )
            if halt.wait(FOLLOW_EVERY_SECONDS):
                return

    with _follower_lock:
        if _follower is None or not _follower.is_alive():
            _follower = threading.Thread(target=run, name="shutdown-switch", daemon=True)
            _follower.start()
        return _follower


def outside_sleep(wait_s: float, now: datetime | None = None) -> float:
    """A wait of `wait_s` seconds from `now`, stretched to the end of the window when it would
    otherwise end inside it."""
    now = now or utc_now()
    return wait_s + seconds_until_wake(now + timedelta(seconds=wait_s))
