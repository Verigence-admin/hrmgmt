from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from hrmgmt.security import HumanPrincipal, SecurityTokenValidator, TokenError

ISSUER = "verigence-security"
AUDIENCE = "verigence-platform"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeJwks:
    class _Key:
        key = _KEY.public_key()
        algorithm_name = "RS256"

    def get_signing_key_from_jwt(self, _token: str):
        return self._Key()


def _validator() -> SecurityTokenValidator:
    return SecurityTokenValidator(
        jwks_url="https://example.invalid/jwks",
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_client=_FakeJwks(),
    )


def _token(**overrides) -> str:
    now = int(time.time())
    claims = {
        "sub": "user-1",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
        "jti": "j1",
        "actor_type": "USER",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, _KEY, algorithm="RS256")


def test_valid_human_token():
    assert _validator().validate_human(_token()) == HumanPrincipal(user_id="user-1")


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": int(time.time()) - 10},
        {"aud": "someone-else"},
        {"iss": "someone-else"},
        {"actor_type": "SERVICE_INTEGRATION"},
        {"sub": " "},
        {"jti": None},
        {"tenant_id": "t1"},
        {"permissions": ["hr.employee.manage"]},
        {"roles": ["CEO"]},
    ],
)
def test_rejected_tokens(overrides):
    with pytest.raises(TokenError):
        _validator().validate_human(_token(**overrides))


def test_garbage_token_rejected():
    with pytest.raises(TokenError):
        _validator().validate_human("not-a-jwt")
