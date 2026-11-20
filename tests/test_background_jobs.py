"""myHR's background jobs wait by the clock; they do not ask the database on a timer
(product owner, 20 Nov 2026: "lets stop polling from myHR")."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from hrmgmt import approver_deadline as ad
from hrmgmt import sleep_window as sw
from hrmgmt import workcontext as wc
from hrmgmt.timeutil import IST

NIGHT = (time(0, 30), time(8, 0))


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=IST).astimezone(UTC)


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class Stop:
    """Stands in for the stop event: records every wait, moves the clock on, stops after `turns`."""

    def __init__(self, clock: Clock, turns: int):
        self.clock, self.turns, self.waits = clock, turns, []

    def is_set(self) -> bool:
        return len(self.waits) >= self.turns

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.clock.now += timedelta(seconds=seconds)
        return self.is_set()


# ---- the sleep window ---------------------------------------------------------------------


def test_the_window_is_read_like_audit_core_reads_it():
    assert sw.parse_window("00:30-08:00") == NIGHT
    assert sw.parse_window("off") is None
    assert sw.parse_window("nonsense") == NIGHT  # a typing mistake never changes the window


def test_seconds_until_wake_is_zero_by_day_and_runs_to_0800_at_night(monkeypatch):
    monkeypatch.setattr(sw, "WINDOW", NIGHT)
    assert sw.seconds_until_wake(at("2026-11-20 12:00")) == 0
    assert sw.seconds_until_wake(at("2026-11-20 00:30")) == 7.5 * 3600
    assert sw.seconds_until_wake(at("2026-11-20 07:59")) == 60
    assert sw.seconds_until_wake(at("2026-11-20 08:00")) == 0
    monkeypatch.setattr(sw, "WINDOW", None)  # "off": never asleep
    assert sw.seconds_until_wake(at("2026-11-20 03:00")) == 0


def test_a_wait_that_would_end_at_night_ends_at_0800(monkeypatch):
    monkeypatch.setattr(sw, "WINDOW", NIGHT)
    now = at("2026-11-20 23:00")
    assert sw.outside_sleep(3600, now) == 3600  # 00:00, before the window
    assert sw.outside_sleep(2 * 3600, now) == 9 * 3600  # 01:00 -> 08:00
    assert sw.outside_sleep(10 * 3600, now) == 10 * 3600  # 09:00, after it


# ---- the Monday 09:00 deadline ------------------------------------------------------------


def test_the_next_deadline_is_the_coming_monday_0900_ist():
    assert ad.next_deadline(at("2026-11-20 17:00")) == at("2026-11-23 09:00")
    assert ad.next_deadline(at("2026-11-23 08:59")) == at("2026-11-23 09:00")
    assert ad.next_deadline(at("2026-11-23 09:00")) == at("2026-11-30 09:00")


def test_the_deadline_job_runs_at_start_and_at_monday_0900_and_never_in_between(monkeypatch):
    monkeypatch.setattr(sw, "WINDOW", NIGHT)
    clock = Clock(at("2026-11-20 17:00"))  # a Friday
    runs: list[datetime] = []
    monkeypatch.setattr(ad, "run_deadline", lambda engine, provisioner, now: runs.append(now))
    job = ad.WeeklyDeadline(None, None, clock=clock)  # type: ignore[arg-type]
    job._stop = Stop(clock, turns=3)  # type: ignore[assignment]
    job._loop()
    # once a minute after start, then a second past each of the next two Mondays 09:00
    assert runs == [at("2026-11-20 17:01"), at("2026-11-23 09:00:01")]
    assert job._stop.waits == [60.0, pytest.approx(230341.0), pytest.approx(7 * 86400)]  # type: ignore[attr-defined]


def test_a_deadline_run_that_failed_is_tried_again_ten_minutes_later(monkeypatch):
    monkeypatch.setattr(sw, "WINDOW", NIGHT)
    clock = Clock(at("2026-11-23 09:05"))
    calls: list[datetime] = []

    def flaky(engine, provisioner, now):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("database unreachable")

    monkeypatch.setattr(ad, "run_deadline", flaky)
    job = ad.WeeklyDeadline(None, None, clock=clock)  # type: ignore[arg-type]
    job._stop = Stop(clock, turns=3)  # type: ignore[assignment]
    job._loop()
    assert calls == [at("2026-11-23 09:06"), at("2026-11-23 09:16")]
    assert job._stop.waits[1] == 600.0  # type: ignore[attr-defined]


def test_a_deadline_job_started_at_night_waits_for_the_morning(monkeypatch):
    monkeypatch.setattr(sw, "WINDOW", NIGHT)
    clock = Clock(at("2026-11-21 02:00"))
    runs: list[datetime] = []
    monkeypatch.setattr(ad, "run_deadline", lambda engine, provisioner, now: runs.append(now))
    job = ad.WeeklyDeadline(None, None, clock=clock)  # type: ignore[arg-type]
    job._stop = Stop(clock, turns=2)  # type: ignore[assignment]
    job._loop()
    assert runs == [at("2026-11-21 08:01")]


# ---- the daily work-context refresh -------------------------------------------------------


class Copy:
    """Stands in for hr.work_sync and the refresh: counts every read of the database."""

    def __init__(self, monkeypatch, clock: Clock, last_success: datetime | None, ok: bool = True):
        self.clock, self.last, self.ok = clock, last_success, ok
        self.reads: list[datetime] = []
        self.refreshes: list[datetime] = []
        monkeypatch.setattr(wc, "sync_status", self._status)
        monkeypatch.setattr(wc, "run_sync", self._run)

    def _status(self, conn):
        self.reads.append(self.clock.now)
        return {"last_success_at": self.last}

    def _run(self, engine, client):
        self.refreshes.append(self.clock.now)
        if self.ok:
            self.last = self.clock.now
        return wc.SyncResult(ok=self.ok)


class Engine:
    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _job(clock: Clock, turns: int) -> wc.DailySync:
    job = wc.DailySync(Engine(), None, clock=clock)  # type: ignore[arg-type]
    job._stop = Stop(clock, turns)  # type: ignore[assignment]
    return job


def test_the_copy_is_refreshed_once_a_day_with_one_read_at_start_and_one_when_due(monkeypatch):
    monkeypatch.setattr(sw, "WINDOW", NIGHT)
    clock = Clock(at("2026-11-20 18:00"))
    copy = Copy(monkeypatch, clock, last_success=at("2026-11-20 10:00"))
    job = _job(clock, turns=4)
    job._loop()
    # start (18:00:30): one read, not due. Due at 10:00 next day: one read, refresh. Then daily.
    assert copy.refreshes == [at("2026-11-21 10:00"), at("2026-11-22 10:00")]
    assert copy.reads == [at("2026-11-20 18:00:30"), at("2026-11-21 10:00"), at("2026-11-22 10:00")]


def test_a_refresh_that_falls_due_at_night_waits_for_0800_and_reads_nothing_before(monkeypatch):
    monkeypatch.setattr(sw, "WINDOW", NIGHT)
    clock = Clock(at("2026-11-20 20:00"))
    copy = Copy(monkeypatch, clock, last_success=at("2026-11-20 02:00"))  # due 02:00, at night
    job = _job(clock, turns=3)
    job._loop()
    assert copy.refreshes == [at("2026-11-21 08:00")]
    assert copy.reads == [at("2026-11-20 20:00:30"), at("2026-11-21 08:00")]


def test_a_service_started_at_night_reads_and_refreshes_nothing_before_0800(monkeypatch):
    monkeypatch.setattr(sw, "WINDOW", NIGHT)
    clock = Clock(at("2026-11-21 01:00"))
    copy = Copy(monkeypatch, clock, last_success=None)
    job = _job(clock, turns=3)
    job._loop()
    assert copy.reads == [at("2026-11-21 08:00")] and copy.refreshes == [at("2026-11-21 08:00")]


def test_a_failed_refresh_is_tried_again_an_hour_later_not_in_a_loop(monkeypatch):
    monkeypatch.setattr(sw, "WINDOW", NIGHT)
    clock = Clock(at("2026-11-20 12:00"))
    copy = Copy(monkeypatch, clock, last_success=None, ok=False)
    job = _job(clock, turns=3)
    job._loop()
    assert copy.refreshes == [at("2026-11-20 12:00:30"), at("2026-11-20 13:00:30")]


def test_a_refresh_hr_ran_by_hand_moves_the_next_one(monkeypatch):
    monkeypatch.setattr(sw, "WINDOW", NIGHT)
    clock = Clock(at("2026-11-20 09:00"))
    copy = Copy(monkeypatch, clock, last_success=at("2026-11-19 10:00"))  # due 10:00 today
    job = _job(clock, turns=3)
    original = job._stop.wait

    def wait_and_refresh_by_hand(seconds: float) -> bool:
        stopped = original(seconds)
        if len(job._stop.waits) == 2:  # type: ignore[attr-defined]
            copy.last = at("2026-11-20 09:30")  # HR pressed Refresh at 09:30
        return stopped

    job._stop.wait = wait_and_refresh_by_hand  # type: ignore[method-assign]
    job._loop()
    assert copy.refreshes == []  # at 10:00 the copy is half an hour old: nothing to do yet
    assert job._stop.waits[-1] == pytest.approx(23.5 * 3600)  # type: ignore[attr-defined]
