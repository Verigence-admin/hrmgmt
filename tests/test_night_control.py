"""Night control on file downloads (product owner, 6 Nov 2026): Security decides, myHR enforces."""

from __future__ import annotations

import dataclasses
import json
import uuid

import httpx
import pytest
from fastapi.routing import APIRoute
from structlog.testing import capture_logs

from hrmgmt import permissions as perm
from hrmgmt.night_control import (
    FILE_DOWNLOAD,
    NightControlUnavailableError,
    NightGate,
    SecurityNightControl,
    night_file_download,
)
from tests.support import (
    NIGHT_MODE_BODY,
    FakeNightControl,
    World,
    install_night,
    jpeg,
)

PROTECTED = {
    "/hr/v1/attendance/{attendance_id}/photo/{event}",
    "/hr/v1/attendance/report",
    "/hr/v1/claims/{claim_id}/receipts/{receipt_id}",
    "/hr/v1/payslips/{payslip_id}/pdf",
    "/hr/v1/tickets/{ticket_id}/files/{file_id}",
}


@pytest.fixture()
def world(migrated_engine):
    return World(migrated_engine)


# ---- the Security client ------------------------------------------------------------------


def _client(handler, token=lambda: "svc-token") -> SecurityNightControl:
    return SecurityNightControl(
        base_url="https://security.test/",
        token_provider=token,
        transport=httpx.MockTransport(handler),
    )


def test_the_client_asks_security_with_the_service_token_and_reads_the_answer():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "featureKey": "FILE_DOWNLOAD",
                "blocked": True,
                "reasonCode": "NIGHT_MODE",
                "message": "Night Mode On",
                "startsAt": "20:00",
                "endsAt": "08:30",
                "allowedBy": None,
            },
        )

    decision = _client(handler).check(user_id="u-1", feature_key=FILE_DOWNLOAD)
    assert decision.blocked is True
    assert decision.reason_code == "NIGHT_MODE" and decision.message == "Night Mode On"
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "https://security.test/security/v1/service/night-control/check"
    assert request.headers["Authorization"] == "Bearer svc-token"
    assert json.loads(request.content) == {"userId": "u-1", "featureKey": "FILE_DOWNLOAD"}


def test_the_client_reads_an_allowed_answer():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"featureKey": "FILE_DOWNLOAD", "blocked": False, "reasonCode": None},
        )

    decision = _client(handler).check(user_id="u-1", feature_key=FILE_DOWNLOAD)
    assert decision.blocked is False and decision.reason_code is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"blocked": True}),
        httpx.Response(404),
        httpx.Response(403, json={"code": "FORBIDDEN"}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"featureKey": "FILE_DOWNLOAD"}),
        httpx.Response(200, json={"blocked": "yes"}),
        httpx.Response(200, json=[]),
    ],
)
def test_the_client_reports_any_unusable_answer_as_unavailable(response):
    with pytest.raises(NightControlUnavailableError):
        _client(lambda request: response).check(user_id="u-1", feature_key=FILE_DOWNLOAD)


def test_the_client_reports_no_connection_or_no_token_as_unavailable():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(NightControlUnavailableError):
        _client(down).check(user_id="u-1", feature_key=FILE_DOWNLOAD)

    def no_token() -> str:
        raise RuntimeError("token endpoint down")

    with pytest.raises(NightControlUnavailableError):
        _client(lambda r: httpx.Response(200, json={"blocked": False}), token=no_token).check(
            user_id="u-1", feature_key=FILE_DOWNLOAD
        )


# ---- the gate: reuse for a minute, allow when Security cannot answer ----------------------


class Ticks:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_decision_is_reused_for_a_minute_per_person():
    fake, ticks = FakeNightControl(blocked=True), Ticks()
    gate = NightGate(fake, monotonic=ticks)
    assert gate.blocked(user_id="a", feature_key=FILE_DOWNLOAD) is True
    fake.blocked = False  # e.g. SuperAdmin grants access
    ticks.now += 59
    assert gate.blocked(user_id="a", feature_key=FILE_DOWNLOAD) is True
    assert gate.blocked(user_id="b", feature_key=FILE_DOWNLOAD) is False  # asked separately
    assert fake.calls == [("a", FILE_DOWNLOAD), ("b", FILE_DOWNLOAD)]
    ticks.now += 2  # past the minute: asked again
    assert gate.blocked(user_id="a", feature_key=FILE_DOWNLOAD) is False
    assert fake.calls[-1] == ("a", FILE_DOWNLOAD) and len(fake.calls) == 3


def test_when_security_cannot_answer_the_download_is_allowed_with_a_warning_and_not_reused():
    fake, ticks = FakeNightControl(blocked=True, error=RuntimeError("down")), Ticks()
    gate = NightGate(fake, monotonic=ticks)
    with capture_logs() as logs:
        assert gate.blocked(user_id="a", feature_key=FILE_DOWNLOAD) is False
    assert any(
        e["event"] == "hr_night_control_unavailable" and e["log_level"] == "warning" for e in logs
    )
    fake.error = None  # Security is back: the very next request asks it again
    assert gate.blocked(user_id="a", feature_key=FILE_DOWNLOAD) is True
    assert len(fake.calls) == 2


# ---- the routes ---------------------------------------------------------------------------


def _api_routes(routes) -> list[APIRoute]:
    """Every endpoint, also inside included routers (this FastAPI wraps them)."""
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        elif hasattr(route, "original_router"):
            found.extend(_api_routes(route.original_router.routes))
    return found


def _guarded_paths(app) -> set[str]:
    return {
        route.path
        for route in _api_routes(app.routes)
        if any(d.dependency is night_file_download for d in route.dependencies)
    }


def test_exactly_the_file_routes_carry_the_night_guard_and_profile_photos_do_not(world):
    assert len(_api_routes(world.app.routes)) > 100  # the walk really sees the endpoints
    guarded = _guarded_paths(world.app)
    assert guarded == PROTECTED
    assert "/hr/v1/employees/{employee_id}/photo" not in guarded
    assert "/hr/v1/me/employee/photo" not in guarded


def _concrete(path: str) -> str:
    return (
        path.replace("{event}", "in")
        .replace("{attendance_id}", str(uuid.uuid4()))
        .replace("{claim_id}", str(uuid.uuid4()))
        .replace("{receipt_id}", str(uuid.uuid4()))
        .replace("{payslip_id}", str(uuid.uuid4()))
        .replace("{ticket_id}", str(uuid.uuid4()))
        .replace("{file_id}", str(uuid.uuid4()))
    )


@pytest.mark.parametrize("path", sorted(PROTECTED))
def test_every_protected_route_refuses_with_night_mode_when_security_says_blocked(world, path):
    user = world.grant(str(uuid.uuid4()), perm.HR_ATTENDANCE_READ_ALL)
    night = install_night(world.app, FakeNightControl(blocked=True))
    r = world.client.get(_concrete(path), headers=world.headers(user))
    assert r.status_code == 403 and r.json() == NIGHT_MODE_BODY
    assert night.calls == [(user, FILE_DOWNLOAD)]

    night.blocked = False  # allowed: the route's own answer (here: nothing with that id)
    r = world.client.get(_concrete(path), headers=world.headers(user))
    assert r.status_code != 403 or r.json()["code"] != "NIGHT_MODE"

    night.error = RuntimeError("Security is down")  # unavailable: allowed too
    r = world.client.get(_concrete(path), headers=world.headers(user))
    assert r.status_code != 403 or r.json()["code"] != "NIGHT_MODE"


@pytest.mark.parametrize("path", sorted(PROTECTED))
def test_without_night_control_configured_nothing_is_refused(world, path):
    assert world.app.state.night_gate is None
    user = world.grant(str(uuid.uuid4()), perm.HR_ATTENDANCE_READ_ALL)
    r = world.client.get(_concrete(path), headers=world.headers(user))
    assert r.status_code != 403 or r.json()["code"] != "NIGHT_MODE"


def test_sign_in_is_checked_before_night_control(world):
    night = install_night(world.app, FakeNightControl(blocked=True))
    r = world.client.get(_concrete("/hr/v1/payslips/{payslip_id}/pdf"))
    assert r.status_code == 401 and r.json()["code"] == "HR_AUTHENTICATION_FAILED"
    assert night.calls == []


def test_profile_photos_are_never_checked(world):
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_READ)
    emp, me = world.employee(hr)
    assert (
        world.client.post(
            f"/hr/v1/employees/{emp['employeeId']}/photo",
            files={"file": ("me.jpg", jpeg(), "image/jpeg")},
            headers=world.headers(hr),
        ).status_code
        == 200
    )
    night = install_night(world.app, FakeNightControl(blocked=True))
    assert (
        world.client.get("/hr/v1/me/employee/photo", headers=world.headers(me)).status_code == 200
    )
    assert (
        world.client.get(
            f"/hr/v1/employees/{emp['employeeId']}/photo", headers=world.headers(hr)
        ).status_code
        == 200
    )
    assert night.calls == []


def test_the_service_wires_night_control_only_with_security_configured():
    from hrmgmt.main import create_app
    from tests.support import settings

    assert create_app(settings()).state.night_gate is None
    configured = dataclasses.replace(
        settings(),
        security_base_url="https://security.test",
        security_client_id="hrmgmt",
        security_client_secret="s",
    )
    assert isinstance(create_app(configured).state.night_gate, NightGate)
