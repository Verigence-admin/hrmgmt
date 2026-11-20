"""HR's own copy of who works where, and who may approve whose request.

The copy comes from Audit Core (project role and assigned outlets), pulled once a day. No HR
request waits on Audit Core: if it is slow or down HR keeps working on the last copy and shows
its age. One call, a short timeout, no retry; a manual refresh is spaced at least five minutes
apart."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
import structlog
from sqlalchemy import Connection, Engine, text

from hrmgmt import sleep_window
from hrmgmt.timeutil import utc_now

logger = structlog.get_logger(__name__)

AUDIENCE = "audit"
MIN_SPACING = timedelta(minutes=5)
DAILY = timedelta(hours=24)
_ROLES = ("PC", "TL", "PM")


class WorkContextError(RuntimeError):
    """Audit Core could not give the work context. The message is safe to show HR."""


class WorkContextClient:
    def __init__(
        self,
        *,
        base_url: str,
        token_provider: Callable[[str], str],
        timeout_seconds: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds, transport=transport
        )
        self._token = token_provider

    def fetch(self) -> list[dict[str, Any]]:
        try:
            token = self._token(AUDIENCE)
            response = self._client.get(
                "/v1/service/hr/work-context", headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise WorkContextError("Audit Core could not be reached.") from exc
        except Exception as exc:  # token problems arrive as AuthzUnavailableError
            raise WorkContextError("A service token for Audit Core could not be obtained.") from exc
        if response.status_code == 403:
            raise WorkContextError("Audit Core does not allow HR to read the work context.")
        if response.status_code != 200:
            raise WorkContextError(f"Audit Core answered HTTP {response.status_code}.")
        try:
            body = response.json()
            rows = body["assignments"]
        except (ValueError, KeyError, TypeError) as exc:
            raise WorkContextError("Audit Core gave an unexpected answer.") from exc
        if not isinstance(rows, list):
            raise WorkContextError("Audit Core gave an unexpected answer.")
        return rows

    def report_leavers(self, leavers: list[dict[str, Any]]) -> int:
        """Tell Audit Core who has left and their last day (2026-11-09). The whole list each
        time: someone made active again drops off it. Returns how many roles Audit Core ended."""
        try:
            token = self._token(AUDIENCE)
            response = self._client.put(
                "/v1/service/hr/employment",
                json={"leavers": leavers},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise WorkContextError("Audit Core could not be reached.") from exc
        except Exception as exc:  # token problems arrive as AuthzUnavailableError
            raise WorkContextError("A service token for Audit Core could not be obtained.") from exc
        if response.status_code != 200:
            raise WorkContextError(f"Audit Core answered HTTP {response.status_code}.")
        try:
            return int(response.json().get("rolesEnded") or 0)
        except (ValueError, TypeError, AttributeError):
            return 0


@dataclass(frozen=True)
class SyncResult:
    ok: bool
    seen: int = 0
    error: str | None = None
    skipped: bool = False


def sync_status(conn: Connection) -> dict[str, Any]:
    row = (
        conn.execute(
            text(
                "SELECT last_attempt_at, last_success_at, last_status, last_error, assignments_seen"
                " FROM hr.work_sync WHERE singleton"
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else {}


def _upsert(conn: Connection, rows: list[dict[str, Any]], now: datetime) -> int:
    seen = 0
    for r in rows:
        try:
            user = str(r["securityUserId"])
            tenant = str(r["tenantId"])
            role = str(r["roleCode"]).upper()
            valid_from = r["effectiveFrom"]
        except (KeyError, TypeError):
            continue
        # Security user ids are UUIDs; an assignment keyed by anything else cannot be an employee.
        try:
            user = str(uuid.UUID(user))
        except ValueError:
            continue
        params = {
            "user": user,
            "tenant": tenant,
            "pcode": r.get("projectCode"),
            "pname": r.get("projectName"),
            "role": role,
            "dealer": r.get("dealerName"),
            "outlet": r.get("outletId"),
            "ocode": r.get("outletCode"),
            "oname": r.get("outletName"),
            "lat": r.get("latitude"),
            "lon": r.get("longitude"),
            "vfrom": valid_from,
            "vto": r.get("effectiveTo"),
            "now": now,
        }
        conn.execute(
            text(
                """
                INSERT INTO hr.work_assignment
                    (security_user_id, tenant_id, project_code, project_name, role_code,
                     dealer_name, outlet_id, outlet_code, outlet_name, latitude, longitude,
                     valid_from, valid_to, last_seen_at)
                VALUES (CAST(:user AS uuid), :tenant, :pcode, :pname, :role, :dealer,
                        CAST(:outlet AS uuid), :ocode, :oname, :lat, :lon,
                        CAST(:vfrom AS timestamptz), CAST(:vto AS timestamptz), :now)
                ON CONFLICT (security_user_id, tenant_id, role_code,
                             coalesce(outlet_id, '00000000-0000-0000-0000-000000000000'::uuid),
                             valid_from)
                DO UPDATE SET project_code = EXCLUDED.project_code,
                              project_name = EXCLUDED.project_name,
                              dealer_name = EXCLUDED.dealer_name,
                              outlet_code = EXCLUDED.outlet_code,
                              outlet_name = EXCLUDED.outlet_name,
                              latitude = EXCLUDED.latitude,
                              longitude = EXCLUDED.longitude,
                              valid_to = EXCLUDED.valid_to,
                              last_seen_at = EXCLUDED.last_seen_at
                """
            ),
            params,
        )
        seen += 1
    # What Audit Core no longer lists has ended: close it, keep the history.
    conn.execute(
        text(
            "UPDATE hr.work_assignment SET valid_to = :now"
            " WHERE last_seen_at < :now AND (valid_to IS NULL OR valid_to > :now)"
        ),
        {"now": now},
    )
    return seen


def run_sync(engine: Engine, client: WorkContextClient, *, force: bool = False) -> SyncResult:
    """One attempt. Never raises; the outcome is stored for HR to see."""
    now = utc_now()
    with engine.begin() as conn:
        state = sync_status(conn)
        last = state.get("last_attempt_at")
        if last is not None and now - last < MIN_SPACING:
            return SyncResult(
                ok=False, skipped=True, error="Tried a moment ago. Wait a few minutes."
            )
        conn.execute(
            text("UPDATE hr.work_sync SET last_attempt_at = :n WHERE singleton"), {"n": now}
        )
    try:
        rows = client.fetch()
    except WorkContextError as exc:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE hr.work_sync SET last_status = 'FAILED', last_error = :e WHERE singleton"
                ),
                {"e": str(exc)},
            )
        logger.warning("hr_work_sync_failed")
        return SyncResult(ok=False, error=str(exc))
    with engine.begin() as conn:
        seen = _upsert(conn, rows, now)
        conn.execute(
            text(
                "UPDATE hr.work_sync SET last_success_at = :n, last_status = 'OK', last_error = NULL,"
                " assignments_seen = :s WHERE singleton"
            ),
            {"n": now, "s": seen},
        )
    logger.info("hr_work_sync_ok", assignments=seen)
    report_leavers(engine, client)
    return SyncResult(ok=True, seen=seen)


def report_leavers(engine: Engine, client: WorkContextClient) -> bool:
    """Send Audit Core who has left (leavers.leavers): after each status change and with the daily
    sync. One attempt; a failure is logged and the next one sends the whole list again."""
    from hrmgmt.leavers import leavers

    try:
        with engine.connect() as conn:
            items = leavers(conn)
        ended = client.report_leavers(items)
    except WorkContextError as exc:
        logger.warning("hr_leavers_report_failed", error=str(exc))
        return False
    except Exception as exc:
        logger.warning("hr_leavers_report_failed", error_type=type(exc).__name__)
        return False
    logger.info("hr_leavers_reported", leavers=len(items), roles_ended=ended)
    return True


class DailySync:
    """Refreshes the copy once a day, by the clock.

    It used to ask the database every hour whether the copy was a day old, day and night; and as
    each refresh came up to an hour later than the last, the refresh itself wandered round the
    clock into the night (20 Nov 2026). Now the job reads when the copy was last refreshed once
    at start and once when a refresh falls due, and asks the database nothing in between. A
    refresh that falls due inside the night's sleep (hrmgmt.sleep_window) waits for the end of
    it. A failed attempt is tried again an hour later; nothing is retried in a loop."""

    def __init__(
        self,
        engine: Engine,
        client: WorkContextClient,
        *,
        retry_s: float = 3600.0,
        start_delay_s: float = 30.0,
        clock: Callable[[], datetime] = utc_now,
    ):
        self._engine = engine
        self._client = client
        self._retry = retry_s
        self._start_delay = start_delay_s
        self._clock = clock
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="hr-work-sync", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def seconds_until_due(self) -> float:
        """How long until the copy is a day old (one read); 0 when it already is, or never was
        refreshed. A refresh HR ran by hand in the meantime counts."""
        with self._engine.connect() as conn:
            last = sync_status(conn).get("last_success_at")
        if last is None:
            return 0.0
        return max(0.0, (last + DAILY - self._clock()).total_seconds())

    def step(self) -> float:
        """One turn: refresh if due, and return how long to wait before the next turn."""
        now = self._clock()
        asleep_for = sleep_window.seconds_until_wake(now)
        if asleep_for > 0:  # started at night: nothing is read or refreshed before the morning
            return asleep_for
        wait = self._retry
        try:
            wait = self.seconds_until_due()
            if wait <= 0:
                refreshed = run_sync(self._engine, self._client).ok
                wait = DAILY.total_seconds() if refreshed else self._retry
        except Exception as exc:
            logger.warning("hr_work_sync_loop_error", error_type=type(exc).__name__)
        return max(1.0, sleep_window.outside_sleep(wait, now))

    def _loop(self) -> None:
        # Let the service finish starting before the first check.
        if self._stop.wait(self._start_delay):
            return
        while not self._stop.is_set():
            if self._stop.wait(self.step()):
                return


# ---- reading the copy -----------------------------------------------------------------------


def employee_user_id(conn: Connection, employee_id: str) -> str | None:
    row = conn.execute(
        text("SELECT security_user_id FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"),
        {"e": employee_id},
    ).first()
    return str(row[0]) if row and row[0] else None


def assignments_at(conn: Connection, user_id: str, at: datetime) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT tenant_id, project_name, role_code, outlet_id, outlet_name, latitude, longitude
            FROM hr.work_assignment
            WHERE security_user_id = CAST(:u AS uuid)
              AND valid_from <= :at AND (valid_to IS NULL OR valid_to > :at)
            """
        ),
        {"u": user_id, "at": at},
    ).mappings()
    return [dict(r) for r in rows]


def project_roles(conn: Connection, employee_id: str, at: datetime) -> set[str]:
    user = employee_user_id(conn, employee_id)
    if user is None:
        return set()
    return {a["role_code"] for a in assignments_at(conn, user, at) if a["role_code"] in _ROLES}


def project_approvers(
    conn: Connection, employee_id: str, at: datetime, roles: tuple[str, ...]
) -> set[str]:
    """Security user ids holding one of `roles` on a project where this employee works."""
    user = employee_user_id(conn, employee_id)
    if user is None:
        return set()
    tenants = sorted({a["tenant_id"] for a in assignments_at(conn, user, at)})
    if not tenants:
        return set()
    rows = conn.execute(
        text(
            """
            SELECT DISTINCT security_user_id FROM hr.work_assignment
            WHERE tenant_id = ANY(:tenants) AND role_code = ANY(:roles)
              AND valid_from <= :at AND (valid_to IS NULL OR valid_to > :at)
              AND security_user_id <> CAST(:me AS uuid)
            """
        ),
        {"tenants": tenants, "roles": list(roles), "at": at, "me": user},
    )
    return {str(r[0]) for r in rows}
