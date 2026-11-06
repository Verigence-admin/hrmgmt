"""Night control (product owner, 6 Nov 2026): from 20:00 to 08:30 IST nobody downloads a Verigence
file unless SuperAdmin gave them access; SuperAdmin and Executive are never blocked.

Security owns the policy and decides (window, exemptions, grants). myHR only asks Security, per
person and feature, and enforces the answer on its own file routes through `night_guard`. A
decision is reused for a short time so one screen does not cost one Security call per file. If
Security cannot answer, the download is allowed and a warning is logged: night control must never
stop the working day because Security is slow or down."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx
import structlog
from fastapi import Depends, Request

from hrmgmt.errors import ApiError
from hrmgmt.principal import current_user
from hrmgmt.security import HumanPrincipal

logger = structlog.get_logger(__name__)

FILE_DOWNLOAD = "FILE_DOWNLOAD"
NIGHT_MODE_CODE = "NIGHT_MODE"
NIGHT_MODE_MESSAGE = "Night Mode On"
REUSE_SECONDS = 60.0


class NightControlUnavailableError(RuntimeError):
    """Security gave no usable answer. The caller allows the action and logs a warning."""


@dataclass(frozen=True)
class NightDecision:
    blocked: bool
    reason_code: str | None = None
    message: str | None = None


class NightControl(Protocol):
    def check(self, *, user_id: str, feature_key: str) -> NightDecision: ...


class SecurityNightControl:
    """Asks Security whether a feature is blocked for a person right now, with HRMgmt's service
    token (the same token the user provisioner uses). One attempt, short timeout, no retries."""

    def __init__(
        self,
        *,
        base_url: str,
        token_provider: Callable[[], str],
        timeout_seconds: float = 3.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds, transport=transport
        )

    def check(self, *, user_id: str, feature_key: str) -> NightDecision:
        try:
            token = self._token_provider()
        except Exception as exc:
            raise NightControlUnavailableError("no service token") from exc
        try:
            response = self._client.post(
                "/security/v1/service/night-control/check",
                headers={"Authorization": f"Bearer {token}"},
                json={"userId": user_id, "featureKey": feature_key},
            )
        except httpx.HTTPError as exc:
            raise NightControlUnavailableError("Security is not reachable") from exc
        if response.status_code != 200:
            raise NightControlUnavailableError(f"Security answered HTTP {response.status_code}")
        try:
            body = response.json()
            blocked = body["blocked"]
        except (ValueError, KeyError, TypeError) as exc:
            raise NightControlUnavailableError("Security returned an unexpected answer") from exc
        if not isinstance(blocked, bool):
            raise NightControlUnavailableError("Security returned an unexpected answer")
        reason, message = body.get("reasonCode"), body.get("message")
        return NightDecision(
            blocked=blocked,
            reason_code=reason if isinstance(reason, str) else None,
            message=message if isinstance(message, str) else None,
        )


class NightGate:
    """Reuses each person's decision for REUSE_SECONDS (blocked or not, so a grant or the end of
    the window shows within a minute). A failure is not reused: the next request asks again."""

    def __init__(
        self,
        control: NightControl,
        *,
        reuse_seconds: float = REUSE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._control = control
        self._reuse = reuse_seconds
        self._monotonic = monotonic
        self._decisions: dict[tuple[str, str], tuple[bool, float]] = {}
        self._lock = threading.Lock()

    def blocked(self, *, user_id: str, feature_key: str) -> bool:
        key = (user_id, feature_key)
        now = self._monotonic()
        with self._lock:
            cached = self._decisions.get(key)
            if cached is not None and cached[1] > now:
                return cached[0]
        try:
            decision = self._control.check(user_id=user_id, feature_key=feature_key)
        except Exception as exc:  # unreachable, refused or malformed: allow and say so
            logger.warning(
                "hr_night_control_unavailable",
                feature=feature_key,
                error_type=type(exc).__name__,
                reason=str(exc),
            )
            return False
        with self._lock:
            self._decisions[key] = (decision.blocked, now + self._reuse)
        return decision.blocked


def night_guard(feature_key: str) -> Callable[..., None]:
    """Route dependency: refuses with 403 NIGHT_MODE when Security says the feature is blocked
    for the caller now. With no night control configured (tests, dev without Security) it allows."""

    def dependency(request: Request, user: HumanPrincipal = Depends(current_user)) -> None:
        gate: NightGate | None = getattr(request.app.state, "night_gate", None)
        if gate is None:
            return
        if gate.blocked(user_id=user.user_id, feature_key=feature_key):
            logger.info("hr_night_mode_refused", feature=feature_key, path=request.url.path)
            raise ApiError(403, NIGHT_MODE_CODE, NIGHT_MODE_MESSAGE)

    return dependency


night_file_download = night_guard(FILE_DOWNLOAD)
