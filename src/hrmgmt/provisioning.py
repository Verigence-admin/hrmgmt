from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx
import structlog

logger = structlog.get_logger(__name__)


class ProvisioningError(RuntimeError):
    """Creating the Verigence login failed. `code` is a short, safe reason for HR to act on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CreatedLogin:
    user_id: str


class UserProvisioner(Protocol):
    def create_user(
        self, *, first_name: str, last_name: str, email: str, mobile: str, password: str
    ) -> CreatedLogin: ...


class SecurityUserProvisioner:
    """Creates an ACTIVE Verigence user (identity-provider account with a verified email, no
    OTP step) by asking Security with HRMgmt's service identity. One attempt only: on any failure
    the employee record is kept, the reason is shown to HR, and HR decides when to try again."""

    def __init__(
        self,
        *,
        base_url: str,
        token_provider: Callable[[], str],
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds, transport=transport
        )

    def create_user(
        self, *, first_name: str, last_name: str, email: str, mobile: str, password: str
    ) -> CreatedLogin:
        try:
            token = self._token_provider()
        except Exception as exc:
            raise ProvisioningError("SECURITY_UNAVAILABLE", "Security is not reachable") from exc
        try:
            response = self._client.post(
                "/security/v1/service/users",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "firstName": first_name,
                    "lastName": last_name,
                    "email": email,
                    "mobile": mobile,
                    "password": password,
                },
            )
        except httpx.HTTPError as exc:
            logger.warning("hr_login_create_failed", reason="endpoint_unavailable")
            raise ProvisioningError("SECURITY_UNAVAILABLE", "Security is not reachable") from exc

        status = response.status_code
        if status == 201:
            try:
                user_id = response.json().get("userId")
            except ValueError:
                user_id = None
            if not isinstance(user_id, str) or not user_id:
                raise ProvisioningError("BAD_RESPONSE", "Security returned an unexpected answer")
            return CreatedLogin(user_id=user_id)
        logger.warning("hr_login_create_failed", http_status=status)
        if status == 409:
            raise ProvisioningError("EMAIL_OR_MOBILE_EXISTS", "A Verigence user already exists")
        if status == 422:
            raise ProvisioningError("CONTACT_NOT_VALID", "Email or mobile was refused by Security")
        if status in (401, 403):
            raise ProvisioningError("NOT_PERMITTED", "HRMgmt is not allowed to create users")
        raise ProvisioningError("SECURITY_UNAVAILABLE", f"Security answered HTTP {status}")
