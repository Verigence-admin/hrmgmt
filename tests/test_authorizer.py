from __future__ import annotations

import json

import httpx
import pytest

from hrmgmt.authz import AuthzUnavailableError, SecurityAuthorizer

USER = "11111111-1111-1111-1111-111111111111"
PERM = "hr.employee.manage"


class _Security:
    """A fake Security that records every call it receives."""

    def __init__(self, *, allowed=True, check_status=200, token_status=200, echo_permission=PERM):
        self.allowed = allowed
        self.check_status = check_status
        self.token_status = token_status
        self.echo_permission = echo_permission
        self.token_calls = 0
        self.check_bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/security/v1/service/token":
            self.token_calls += 1
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"code": "X"})
            return httpx.Response(
                200,
                json={
                    "accessToken": "svc",
                    "tokenType": "Bearer",
                    "audience": "security",
                    "expiresIn": 900,
                },
            )
        assert request.headers["authorization"] == "Bearer svc"
        body = json.loads(request.content)
        self.check_bodies.append(body)
        if self.check_status != 200:
            return httpx.Response(self.check_status, json={"code": "X"})
        return httpx.Response(
            200,
            json={
                "allowed": self.allowed,
                "userId": USER,
                "permissionKey": self.echo_permission,
                "reasonCode": "R",
            },
        )


def _authorizer(fake: _Security) -> SecurityAuthorizer:
    return SecurityAuthorizer(
        base_url="https://security.invalid",
        client_id="hrmgmt",
        client_secret="secret",
        transport=httpx.MockTransport(fake),
    )


def test_asks_without_a_project_and_reuses_allow_and_token():
    fake = _Security(allowed=True)
    authz = _authorizer(fake)
    assert authz.is_allowed(user_id=USER, permission_key=PERM) is True
    assert authz.is_allowed(user_id=USER, permission_key=PERM) is True
    assert fake.check_bodies == [{"userId": USER, "tenantId": None, "permissionKey": PERM}]
    assert fake.token_calls == 1


def test_deny_is_never_cached():
    fake = _Security(allowed=False)
    authz = _authorizer(fake)
    assert authz.is_allowed(user_id=USER, permission_key=PERM) is False
    assert authz.is_allowed(user_id=USER, permission_key=PERM) is False
    assert len(fake.check_bodies) == 2


@pytest.mark.parametrize(
    "fake",
    [
        _Security(check_status=500),
        _Security(token_status=503),
        _Security(echo_permission="hr.something.else"),
    ],
)
def test_untrustworthy_answers_are_unavailable_and_not_retried(fake):
    authz = _authorizer(fake)
    with pytest.raises(AuthzUnavailableError):
        authz.is_allowed(user_id=USER, permission_key=PERM)
    assert len(fake.check_bodies) <= 1
    assert fake.token_calls == 1
