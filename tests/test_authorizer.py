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


class _BatchSecurity:
    """A fake Security that answers the batch check, and records every call it gets."""

    def __init__(self, *, allowed=(), status=200, wrong_user=False, short=False):
        self.allowed = set(allowed)
        self.status = status
        self.wrong_user = wrong_user
        self.short = short
        self.token_calls = 0
        self.batch_bodies: list[dict] = []
        self.single_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/security/v1/service/token":
            self.token_calls += 1
            return httpx.Response(
                200,
                json={
                    "accessToken": "svc",
                    "tokenType": "Bearer",
                    "audience": "security",
                    "expiresIn": 900,
                },
            )
        if request.url.path == "/security/v1/authorization/check":
            self.single_calls += 1
            return httpx.Response(500)
        assert request.url.path == "/security/v1/authorization/check-batch"
        assert request.headers["authorization"] == "Bearer svc"
        body = json.loads(request.content)
        self.batch_bodies.append(body)
        if self.status != 200:
            return httpx.Response(self.status, json={"code": "X"})
        keys = body["permissionKeys"][:-1] if self.short else body["permissionKeys"]
        return httpx.Response(
            200,
            json={
                "decisions": [
                    {
                        "allowed": key in self.allowed,
                        "userId": "someone-else" if self.wrong_user else USER,
                        "permissionKey": key,
                        "reasonCode": "R",
                    }
                    for key in keys
                ]
            },
        )


def _batch_authorizer(fake: _BatchSecurity) -> SecurityAuthorizer:
    return SecurityAuthorizer(
        base_url="https://security.invalid",
        client_id="hrmgmt",
        client_secret="secret",
        transport=httpx.MockTransport(fake),
    )


KEYS = ["hr.a", "hr.b", "hr.c"]


def test_many_permissions_cost_one_call_to_security():
    fake = _BatchSecurity(allowed={"hr.a"})
    authz = _batch_authorizer(fake)
    got = authz.are_allowed(user_id=USER, permission_keys=KEYS)
    assert got == {"hr.a": True, "hr.b": False, "hr.c": False}
    assert fake.batch_bodies == [{"userId": USER, "tenantId": None, "permissionKeys": KEYS}]
    assert fake.single_calls == 0
    assert fake.token_calls == 1


def test_batch_reuses_an_allow_but_never_a_deny():
    fake = _BatchSecurity(allowed={"hr.a"})
    authz = _batch_authorizer(fake)
    authz.are_allowed(user_id=USER, permission_keys=KEYS)
    authz.are_allowed(user_id=USER, permission_keys=KEYS)
    # the second time only the two that were denied are asked again; hr.a is reused
    assert fake.batch_bodies[1]["permissionKeys"] == ["hr.b", "hr.c"]


def test_batch_with_everything_remembered_makes_no_call():
    fake = _BatchSecurity(allowed=set(KEYS))
    authz = _batch_authorizer(fake)
    authz.are_allowed(user_id=USER, permission_keys=KEYS)
    assert authz.are_allowed(user_id=USER, permission_keys=KEYS) == dict.fromkeys(KEYS, True)
    assert len(fake.batch_bodies) == 1


def test_batch_missing_on_an_older_security_is_told_apart_from_an_outage():
    from hrmgmt.authz import BatchUnavailableError

    for status in (404, 405):
        authz = _batch_authorizer(_BatchSecurity(status=status))
        with pytest.raises(BatchUnavailableError):
            authz.are_allowed(user_id=USER, permission_keys=KEYS)
    outage = _batch_authorizer(_BatchSecurity(status=503))
    with pytest.raises(AuthzUnavailableError) as raised:
        outage.are_allowed(user_id=USER, permission_keys=KEYS)
    assert not isinstance(raised.value, BatchUnavailableError)


def test_batch_refuses_an_answer_that_does_not_match_the_question():
    for fake in (_BatchSecurity(wrong_user=True), _BatchSecurity(short=True)):
        authz = _batch_authorizer(fake)
        with pytest.raises(AuthzUnavailableError):
            authz.are_allowed(user_id=USER, permission_keys=KEYS)
