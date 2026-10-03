from __future__ import annotations

from dataclasses import dataclass

import jwt
from jwt import PyJWKClient, PyJWKClientConnectionError
from jwt.exceptions import PyJWTError


class TokenError(RuntimeError):
    """The bearer token is not a valid Security human token."""


class KeysUnavailableError(RuntimeError):
    """Security's signing keys could not be fetched: the token was not judged (503, not 401)."""


@dataclass(frozen=True)
class HumanPrincipal:
    user_id: str


# Same budgets as Audit Core: keep the key set warm for five minutes and never wait more than
# five seconds for it. PyJWKClient still refetches when it sees an unknown signing key id.
_JWKS_CACHE_LIFESPAN_SECONDS = 300
_JWKS_REQUEST_TIMEOUT_SECONDS = 5.0

# A human token proves who the person is and nothing else. Authority always comes from a
# live Security decision, so a token carrying any of these is refused outright.
_FORBIDDEN_AUTHORITY_CLAIMS = frozenset({"tenant_id", "permissions", "roles", "location_id", "act"})


class SecurityTokenValidator:
    def __init__(
        self,
        *,
        jwks_url: str,
        issuer: str,
        audience: str,
        jwks_client: PyJWKClient | None = None,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks_client = jwks_client or PyJWKClient(
            jwks_url,
            cache_keys=True,
            cache_jwk_set=True,
            lifespan=_JWKS_CACHE_LIFESPAN_SECONDS,
            timeout=_JWKS_REQUEST_TIMEOUT_SECONDS,
        )

    def validate_human(self, token: str) -> HumanPrincipal:
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=[signing_key.algorithm_name],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["exp", "iss", "aud", "sub", "iat", "jti", "actor_type"]},
            )
        except PyJWKClientConnectionError as exc:
            raise KeysUnavailableError("Security signing keys are unavailable") from exc
        except (PyJWTError, ValueError, TypeError) as exc:
            raise TokenError("Invalid Security token") from exc

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise TokenError("Invalid Security token claims")
        if claims.get("actor_type") != "USER":
            raise TokenError("Security token is not a human USER token")
        if _FORBIDDEN_AUTHORITY_CLAIMS.intersection(claims):
            raise TokenError("Security token carries unsupported authority claims")
        return HumanPrincipal(user_id=subject.strip())
