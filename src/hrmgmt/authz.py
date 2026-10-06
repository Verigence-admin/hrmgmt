from __future__ import annotations

import threading
import time
from collections.abc import Sequence
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


class BatchUnavailableError(AuthzUnavailableError):
    """This Security does not offer the batch check (an older Security). The caller asks one by one."""


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
        self._tokens: dict[str, tuple[str, float]] = {}
        self._token_lock = threading.Lock()
        self._allow: dict[tuple[str, str], float] = {}
        self._allow_lock = threading.Lock()

    def close(self) -> None:
        self._token_client.close()
        self._client.close()

    def service_token(self, audience: str = "security") -> str:
        """A service token for one audience (Security's own, or another service such as Audit
        Core), reused until shortly before it expires. One attempt, no retries."""
        with self._token_lock:
            cached = self._tokens.get(audience)
            if cached and time.monotonic() < cached[1]:
                return cached[0]
            try:
                response = self._token_client.post(
                    "/security/v1/service/token", data={"audience": audience}
                )
            except httpx.HTTPError as exc:
                logger.warning("hr_security_token_failed", reason="endpoint_unavailable")
                raise AuthzUnavailableError("Security token endpoint is unavailable") from exc
            if response.status_code != 200:
                logger.warning(
                    "hr_security_token_failed", http_status=response.status_code, audience=audience
                )
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
                or payload.get("audience") != audience
            ):
                raise AuthzUnavailableError("Security token response is not valid")
            reuse = _DEFAULT_TOKEN_REUSE_SECONDS
            if isinstance(expires_in, int) and not isinstance(expires_in, bool) and expires_in > 0:
                reuse = max(0.0, expires_in - max(_TOKEN_SKEW_SECONDS, expires_in * 0.1))
            self._tokens[audience] = (token, time.monotonic() + reuse)
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

    def are_allowed(
        self,
        *,
        user_id: str,
        permission_keys: Sequence[str],
        timeout: float | None = None,
        use_remembered: bool = True,
    ) -> dict[str, bool]:
        """The answers for many HR permissions in ONE call to Security, instead of one call each.
        Same rules as is_allowed: a remembered ALLOW is reused, a DENY and errors never are, and
        there is one attempt with no retries. An answer that does not match the question is refused."""
        if not user_id or not permission_keys:
            raise ValueError("user_id and permission_keys are required")
        keys = list(dict.fromkeys(permission_keys))
        now = time.monotonic()
        answers: dict[str, bool] = {}
        pending: list[str] = []
        with self._allow_lock:
            for key in keys:
                until = self._allow.get((user_id, key)) if use_remembered else None
                if until is not None and until > now:
                    answers[key] = True
                    continue
                if until is not None:
                    self._allow.pop((user_id, key), None)
                pending.append(key)
        if not pending:
            return answers

        token = self.service_token()
        try:
            response = self._client.post(
                "/security/v1/authorization/check-batch",
                headers={"Authorization": f"Bearer {token}"},
                json={"userId": user_id, "tenantId": None, "permissionKeys": pending},
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.HTTPError as exc:
            logger.warning("hr_security_check_failed", reason="endpoint_unavailable", batch=True)
            raise AuthzUnavailableError("Security authorization endpoint is unavailable") from exc
        if response.status_code in (404, 405):
            raise BatchUnavailableError("Security has no batch authorization check")
        if response.status_code != 200:
            logger.warning("hr_security_check_failed", http_status=response.status_code, batch=True)
            raise AuthzUnavailableError(
                f"Security authorization failed (HTTP {response.status_code})"
            )

        payload = _json_object(response, "authorization")
        decisions = payload.get("decisions")
        if not isinstance(decisions, list) or len(decisions) != len(pending):
            raise AuthzUnavailableError(
                "Security authorization response does not match the request"
            )
        for key, decision in zip(pending, decisions, strict=True):
            if (
                not isinstance(decision, dict)
                or not isinstance(decision.get("allowed"), bool)
                or decision.get("userId") != user_id
                or decision.get("permissionKey") != key
            ):
                raise AuthzUnavailableError(
                    "Security authorization response does not match the request"
                )
            answers[key] = decision["allowed"]
        with self._allow_lock:
            until = time.monotonic() + _ALLOW_REUSE_SECONDS
            for key in pending:
                if answers[key]:
                    self._allow[(user_id, key)] = until
        return answers


def _json_object(response: httpx.Response, what: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise AuthzUnavailableError(f"Security {what} response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AuthzUnavailableError(f"Security {what} response has invalid shape")
    return payload
