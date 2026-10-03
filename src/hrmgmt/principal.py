from __future__ import annotations

from collections.abc import Callable

import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from hrmgmt.authz import Authorizer, AuthzUnavailableError
from hrmgmt.errors import dependency_unavailable, forbidden, unauthenticated
from hrmgmt.security import HumanPrincipal, KeysUnavailableError, SecurityTokenValidator, TokenError

logger = structlog.get_logger(__name__)

_bearer = HTTPBearer(auto_error=False)


def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> HumanPrincipal:
    """Who is calling, from a valid Security human token. Grants no permission by itself."""
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not credentials.credentials.strip()
    ):
        raise unauthenticated()
    validator: SecurityTokenValidator | None = getattr(request.app.state, "validator", None)
    if validator is None:
        raise dependency_unavailable("Sign-in verification is not configured.")
    try:
        return validator.validate_human(credentials.credentials.strip())
    except TokenError as exc:
        raise unauthenticated() from exc
    except KeysUnavailableError as exc:
        logger.warning("hr_security_keys_unavailable")
        raise dependency_unavailable("Sign-in verification is temporarily unavailable.") from exc


def get_authorizer(request: Request) -> Authorizer:
    authorizer: Authorizer | None = getattr(request.app.state, "authorizer", None)
    if authorizer is None:
        raise dependency_unavailable("Permission checks are not configured.")
    return authorizer


def has_permission(authorizer: Authorizer, user: HumanPrincipal, permission_key: str) -> bool:
    try:
        return authorizer.is_allowed(user_id=user.user_id, permission_key=permission_key)
    except AuthzUnavailableError as exc:
        raise dependency_unavailable("Permission check is temporarily unavailable.") from exc


def require_permission(permission_key: str) -> Callable[..., HumanPrincipal]:
    """Dependency: the caller must hold this HR permission, decided live by Security."""

    def dependency(
        user: HumanPrincipal = Depends(current_user),
        authorizer: Authorizer = Depends(get_authorizer),
    ) -> HumanPrincipal:
        if not has_permission(authorizer, user, permission_key):
            logger.info("hr_permission_denied", permission=permission_key)
            raise forbidden()
        return user

    return dependency
