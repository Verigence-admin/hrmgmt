from __future__ import annotations

import threading
import time
from typing import Any, Protocol

import httpx
import structlog

logger = structlog.get_logger(__name__)

# A successful ALLOW is reused for a short time so one screen load does not cost one Security
# call per request. DENY and errors are never cached, so a granted permission shows up at once;
# a revoked one stops working within this window. Kept short because these are payroll powers.
_ALLOW_REUSE_SECONDS = 60.0
_TOKEN_SKEW_SECONDS = 60.0
_DEFAULT_TOKEN_REUSE_SECONDS = 60.0


class AuthzUnavailableError(RuntimeError):
    """Security could not give a trustworthy decision. Callers answer 503; they never guess."""


class Authorizer(Protocol):
    def is_allowed(self, *, user_id: str, permission_key: str) -> bool: ...


class SecurityAuthorizer:
    """Asks Security for a company-wide (no project) decision on an HR permission.

    HR access is not part of any project: the question sent is only "may this user do this
    HR permission", with tenantId null. Single attempt per call, no retries: a failure is
    reported as unavailable and the user decides when to try again.
    """

    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.strip() or not client_id.strip() or not client_secret:
            raise ValueError("Security authorization configuration is required")
        base = base_url.rstrip("/")
        self._token_client = httpx.Client(
            base_url=base,
            auth=(client_id, client_secret),
            timeout=timeout_seconds,
            transport=transport,
        )
        self._client = httpx.Client(base_url=base, timeout=timeout_seconds, transport=transport)
        self._token: str | None = None
        self._token_until = 0.0
        self._token_lock = threading.Lock()
        self._allow: dict[tuple[str, str], float] = {}
        self._allow_lock = threading.Lock()

    def close(self) -> None:
        self._token_client.close()
        self._client.close()

    def service_token(self) -> str:
        with self._token_lock:
            if self._token and time.monotonic() < self._token_until:
                return self._token
            try:
                response = self._token_client.post(
                    "/security/v1/service/token", data={"audience": "security"}
                )
            except httpx.HTTPError as exc:
                logger.warning("hr_security_token_failed", reason="endpoint_unavailable")
                raise AuthzUnavailableError("Security token endpoint is unavailable") from exc
            if response.status_code != 200:
                logger.warning("hr_security_token_failed", http_status=response.status_code)
                raise AuthzUnavailableError(
                    f"Security token request failed (HTTP {response.status_code})"
                )
            payload = _json_object(response, "service token")
            token = payload.get("accessToken")
            expires_in = payload.get("expiresIn")
            if (
                not isinstance(token, str)
                or not token
                or payload.get("tokenType") != "Bearer"
                or payload.get("audience") != "security"
            ):
                raise AuthzUnavailableError("Security token response is not valid")
            reuse = _DEFAULT_TOKEN_REUSE_SECONDS
            if isinstance(expires_in, int) and not isinstance(expires_in, bool) and expires_in > 0:
                reuse = max(0.0, expires_in - max(_TOKEN_SKEW_SECONDS, expires_in * 0.1))
            self._token = token
            self._token_until = time.monotonic() + reuse
            return token

    def is_allowed(self, *, user_id: str, permission_key: str) -> bool:
        if not user_id or not permission_key:
            raise ValueError("user_id and permission_key are required")
        key = (user_id, permission_key)
        now = time.monotonic()
        with self._allow_lock:
            until = self._allow.get(key)
            if until is not None:
                if until > now:
                    return True
                self._allow.pop(key, None)

        token = self.service_token()
        try:
            response = self._client.post(
                "/security/v1/authorization/check",
                headers={"Authorization": f"Bearer {token}"},
                json={"userId": user_id, "tenantId": None, "permissionKey": permission_key},
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "hr_security_check_failed", reason="endpoint_unavailable", permission=permission_key
            )
            raise AuthzUnavailableError("Security authorization endpoint is unavailable") from exc
        if response.status_code != 200:
            logger.warning(
                "hr_security_check_failed",
                http_status=response.status_code,
                permission=permission_key,
            )
            raise AuthzUnavailableError(
                f"Security authorization failed (HTTP {response.status_code})"
            )

        payload = _json_object(response, "authorization")
        allowed = payload.get("allowed")
        if (
            not isinstance(allowed, bool)
            or payload.get("userId") != user_id
            or payload.get("permissionKey") != permission_key
        ):
            raise AuthzUnavailableError(
                "Security authorization response does not match the request"
            )
        if allowed:
            with self._allow_lock:
                self._allow[key] = time.monotonic() + _ALLOW_REUSE_SECONDS
        return allowed


def _json_object(response: httpx.Response, what: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise AuthzUnavailableError(f"Security {what} response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AuthzUnavailableError(f"Security {what} response has invalid shape")
    return payload
