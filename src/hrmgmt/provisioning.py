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


@dataclass(frozen=True)
class FoundLogin:
    user_id: str
    display_name: str | None
    status: str


@dataclass(frozen=True)
class UserSummary:
    user_id: str
    display_name: str | None
    email: str | None
    status: str
    is_employee: bool


@dataclass(frozen=True)
class SyncOutcome:
    user_id: str
    found: bool
    status: str | None
    ticked: bool
    suspended: bool
    note: str | None


class UserProvisioner(Protocol):
    def create_user(
        self, *, first_name: str, last_name: str, email: str, mobile: str, password: str
    ) -> CreatedLogin: ...

    def find_user(self, *, email: str) -> FoundLogin | None: ...

    def mark_employee(self, *, user_id: str) -> None: ...

    def set_password(self, *, user_id: str, password: str) -> str | None: ...

    def list_users(
        self,
        *,
        q: str | None = None,
        ids: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[UserSummary]: ...


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

    def find_user(self, *, email: str) -> FoundLogin | None:
        """The existing Verigence user with this email, or None. One attempt, no retries."""
        try:
            token = self._token_provider()
        except Exception as exc:
            raise ProvisioningError("SECURITY_UNAVAILABLE", "Security is not reachable") from exc
        try:
            response = self._client.get(
                "/security/v1/service/users/lookup",
                headers={"Authorization": f"Bearer {token}"},
                params={"email": email},
            )
        except httpx.HTTPError as exc:
            logger.warning("hr_login_lookup_failed", reason="endpoint_unavailable")
            raise ProvisioningError("SECURITY_UNAVAILABLE", "Security is not reachable") from exc
        if response.status_code == 404:
            return None
        if response.status_code in (401, 403):
            raise ProvisioningError("NOT_PERMITTED", "HRMgmt is not allowed to look up users")
        if response.status_code != 200:
            logger.warning("hr_login_lookup_failed", http_status=response.status_code)
            raise ProvisioningError(
                "SECURITY_UNAVAILABLE", f"Security answered HTTP {response.status_code}"
            )
        try:
            body = response.json()
            user_id, status = body["userId"], body["status"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProvisioningError(
                "BAD_RESPONSE", "Security returned an unexpected answer"
            ) from exc
        if not isinstance(user_id, str) or not user_id or not isinstance(status, str):
            raise ProvisioningError("BAD_RESPONSE", "Security returned an unexpected answer")
        name = body.get("displayName")
        return FoundLogin(
            user_id=user_id, display_name=name if isinstance(name, str) else None, status=status
        )

    def mark_employee(self, *, user_id: str) -> None:
        """Ticks "Is Employee" on an existing Verigence user that was just linked to an employee."""
        try:
            token = self._token_provider()
        except Exception as exc:
            raise ProvisioningError("SECURITY_UNAVAILABLE", "Security is not reachable") from exc
        try:
            response = self._client.post(
                f"/security/v1/service/users/{user_id}/employee",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            logger.warning("hr_mark_employee_failed", reason="endpoint_unavailable")
            raise ProvisioningError("SECURITY_UNAVAILABLE", "Security is not reachable") from exc
        if response.status_code in (401, 403):
            raise ProvisioningError("NOT_PERMITTED", "HRMgmt is not allowed to update users")
        if response.status_code != 200:
            logger.warning("hr_mark_employee_failed", http_status=response.status_code)
            raise ProvisioningError(
                "SECURITY_UNAVAILABLE", f"Security answered HTTP {response.status_code}"
            )

    def set_password(self, *, user_id: str, password: str) -> str | None:
        """Sets a temporary password for an ACTIVE user and returns their sign-in email.
        Security refuses (409) a user who is not active yet. One attempt, no retries."""
        try:
            token = self._token_provider()
        except Exception as exc:
            raise ProvisioningError("SECURITY_UNAVAILABLE", "Security is not reachable") from exc
        try:
            response = self._client.post(
                f"/security/v1/service/users/{user_id}/password",
                headers={"Authorization": f"Bearer {token}"},
                json={"password": password},
            )
        except httpx.HTTPError as exc:
            logger.warning("hr_set_password_failed", reason="endpoint_unavailable")
            raise ProvisioningError("SECURITY_UNAVAILABLE", "Security is not reachable") from exc
        status = response.status_code
        if status == 200:
            try:
                email = response.json().get("primaryEmail")
            except ValueError:
                email = None
            return email if isinstance(email, str) and email else None
        logger.warning("hr_set_password_failed", http_status=status)
        if status == 409:
            raise ProvisioningError("LOGIN_NOT_ACTIVE", "The login is not active yet")
        if status == 404:
            raise ProvisioningError("LOGIN_NOT_FOUND", "The login was not found")
        if status in (401, 403):
            raise ProvisioningError("NOT_PERMITTED", "HRMgmt is not allowed to set passwords")
        raise ProvisioningError("SECURITY_UNAVAILABLE", f"Security answered HTTP {status}")

    def list_users(
        self,
        *,
        q: str | None = None,
        ids: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[UserSummary]:
        """Verigence users (id, name, email, status, Is Employee), by search or by id. One attempt."""
        try:
            token = self._token_provider()
        except Exception as exc:
            raise ProvisioningError("SECURITY_UNAVAILABLE", "Security is not reachable") from exc
        params: dict[str, str | int] = {"limit": limit, "offset": offset}
        if q:
            params["q"] = q
        if ids:
            params["ids"] = ",".join(ids)
        try:
            response = self._client.get(
                "/security/v1/service/users",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
        except httpx.HTTPError as exc:
            logger.warning("hr_list_users_failed", reason="endpoint_unavailable")
            raise ProvisioningError("SECURITY_UNAVAILABLE", "Security is not reachable") from exc
        if response.status_code in (401, 403):
            raise ProvisioningError("NOT_PERMITTED", "HRMgmt is not allowed to list users")
        if response.status_code != 200:
            logger.warning("hr_list_users_failed", http_status=response.status_code)
            raise ProvisioningError(
                "SECURITY_UNAVAILABLE", f"Security answered HTTP {response.status_code}"
            )
        try:
            rows = response.json()
            return [
                UserSummary(
                    user_id=str(r["userId"]),
                    display_name=r.get("displayName"),
                    email=r.get("primaryEmail"),
                    status=str(r["status"]),
                    is_employee=bool(r.get("isEmployee")),
                )
                for r in rows
            ]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProvisioningError(
                "BAD_RESPONSE", "Security returned an unexpected answer"
            ) from exc

    def sync_employees(self, *, items: list[tuple[str, bool]]) -> list[SyncOutcome]:
        """Tells Security which users are employees (user id, suspend?). Security ticks Is Employee
        and suspends an ACTIVE user when asked; it never reactivates. One attempt, up to 100 users."""
        try:
            token = self._token_provider()
        except Exception as exc:
            raise ProvisioningError("SECURITY_UNAVAILABLE", "Security is not reachable") from exc
        try:
            response = self._client.post(
                "/security/v1/service/users/employee-sync",
                headers={"Authorization": f"Bearer {token}"},
                json={"items": [{"userId": uid, "suspend": suspend} for uid, suspend in items]},
                timeout=60.0,
            )
        except httpx.HTTPError as exc:
            logger.warning("hr_employee_sync_failed", reason="endpoint_unavailable")
            raise ProvisioningError("SECURITY_UNAVAILABLE", "Security is not reachable") from exc
        if response.status_code in (401, 403):
            raise ProvisioningError("NOT_PERMITTED", "HRMgmt is not allowed to sync users")
        if response.status_code != 200:
            logger.warning("hr_employee_sync_failed", http_status=response.status_code)
            raise ProvisioningError(
                "SECURITY_UNAVAILABLE", f"Security answered HTTP {response.status_code}"
            )
        try:
            return [
                SyncOutcome(
                    user_id=str(r["userId"]),
                    found=bool(r["found"]),
                    status=r.get("status"),
                    ticked=bool(r["ticked"]),
                    suspended=bool(r["suspended"]),
                    note=r.get("note"),
                )
                for r in response.json()
            ]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProvisioningError(
                "BAD_RESPONSE", "Security returned an unexpected answer"
            ) from exc
