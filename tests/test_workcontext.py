from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from hrmgmt import permissions as perm
from hrmgmt import workcontext as wc
from tests.support import World

USER = str(uuid.uuid4())
TL = str(uuid.uuid4())


def row(user=USER, role="PC", outlet="Cuttack Motors", lat=20.4625, lon=85.8828, **over):
    base = {
        "securityUserId": user,
        "tenantId": "tenant-a",
        "projectCode": "P1",
        "projectName": "Project One",
        "roleCode": role,
        "dealerId": None,
        "dealerName": "Dealer",
        "outletId": str(uuid.uuid5(uuid.NAMESPACE_DNS, outlet)) if outlet else None,
        "outletCode": "O1" if outlet else None,
        "outletName": outlet,
        "latitude": lat if outlet else None,
        "longitude": lon if outlet else None,
        "effectiveFrom": "2026-01-01T00:00:00+00:00",
        "effectiveTo": None,
    }
    base.update(over)
    return base


def client_for(rows, status=200, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if status != 200:
            return httpx.Response(status, json={})
        return httpx.Response(200, json={"generatedAt": "x", "assignments": rows})

    return wc.WorkContextClient(
        base_url="http://audit.test",
        token_provider=lambda audience: f"token-for-{audience}",
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture()
def clean(migrated_engine):
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM hr.work_assignment"))
        conn.execute(
            text(
                "UPDATE hr.work_sync SET last_attempt_at = NULL, last_success_at = NULL, last_status = NULL"
            )
        )
    return migrated_engine


def active(engine):
    with engine.connect() as conn:
        return {
            (str(r[0]), r[1])
            for r in conn.execute(
                text(
                    "SELECT security_user_id, role_code FROM hr.work_assignment WHERE valid_to IS NULL"
                )
            )
        }


def test_sync_stores_assignments_and_asks_for_the_audit_audience(clean):
    calls: list[httpx.Request] = []
    result = wc.run_sync(
        clean, client_for([row(), row(user=TL, role="TL", outlet=None)], calls=calls)
    )
    assert result.ok and result.seen == 2
    assert calls[0].headers["Authorization"] == "Bearer token-for-audit"
    assert calls[0].url.path == "/v1/service/hr/work-context"
    assert active(clean) == {(USER, "PC"), (TL, "TL")}
    with clean.connect() as conn:
        state = wc.sync_status(conn)
    assert state["last_status"] == "OK" and state["assignments_seen"] == 2


def test_running_it_again_updates_without_duplicating_and_closes_what_vanished(clean):
    wc.run_sync(clean, client_for([row(), row(user=TL, role="TL", outlet=None)]))
    with clean.begin() as conn:  # lift the five-minute spacing
        conn.execute(
            text("UPDATE hr.work_sync SET last_attempt_at = now() - interval '10 minutes'")
        )
    wc.run_sync(clean, client_for([row(lat=20.47)]))  # the TL is gone, the outlet moved a little
    assert active(clean) == {(USER, "PC")}
    with clean.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM hr.work_assignment")).scalar_one() == 2
        assert float(
            conn.execute(
                text("SELECT latitude FROM hr.work_assignment WHERE role_code = 'PC'")
            ).scalar_one()
        ) == pytest.approx(20.47)
        closed = conn.execute(
            text("SELECT valid_to FROM hr.work_assignment WHERE role_code = 'TL'")
        ).scalar_one()
    assert closed is not None  # kept as history, not deleted


def test_a_failed_pull_keeps_the_old_copy_and_says_why(clean):
    wc.run_sync(clean, client_for([row()]))
    with clean.begin() as conn:
        conn.execute(
            text("UPDATE hr.work_sync SET last_attempt_at = now() - interval '10 minutes'")
        )
    result = wc.run_sync(clean, client_for([], status=503))
    assert not result.ok and "503" in result.error
    assert active(clean) == {(USER, "PC")}
    with clean.connect() as conn:
        state = wc.sync_status(conn)
    assert state["last_status"] == "FAILED" and state["last_success_at"] is not None


def test_nothing_is_tried_twice_within_five_minutes(clean):
    calls: list[httpx.Request] = []
    client = client_for([row()], calls=calls)
    assert wc.run_sync(clean, client).ok
    again = wc.run_sync(clean, client)
    assert again.skipped and len(calls) == 1


def test_ids_that_are_not_user_ids_are_ignored(clean):
    result = wc.run_sync(clean, client_for([row(user="not-a-uuid"), row()]))
    assert result.ok and result.seen == 1


def test_an_unreadable_answer_is_reported(clean):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"oops": 1}))
    client = wc.WorkContextClient(
        base_url="http://a.test", token_provider=lambda a: "t", transport=transport
    )
    result = wc.run_sync(clean, client)
    assert not result.ok and "unexpected" in result.error


def test_token_trouble_is_reported_not_raised(clean):
    def boom(audience: str) -> str:
        raise RuntimeError("no token")

    client = wc.WorkContextClient(
        base_url="http://a.test",
        token_provider=boom,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    assert not wc.run_sync(clean, client).ok


def test_past_days_are_judged_on_the_assignment_valid_then(clean):
    start = datetime(2026, 3, 1, tzinfo=UTC)
    with clean.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hr.work_assignment (security_user_id, tenant_id, role_code, valid_from,"
                " valid_to, last_seen_at) VALUES (CAST(:u AS uuid), 't', 'PC', :a, :b, now())"
            ),
            {"u": USER, "a": start, "b": start + timedelta(days=30)},
        )
    with clean.connect() as conn:
        assert wc.assignments_at(conn, USER, start + timedelta(days=5))
        assert not wc.assignments_at(conn, USER, start + timedelta(days=45))


def test_refresh_endpoint_needs_settings_permission_and_is_spaced(migrated_engine, clean):
    world = World(migrated_engine)
    hr = world.grant(str(uuid.uuid4()), perm.HR_SETTINGS_MANAGE)
    nobody = str(uuid.uuid4())
    assert (
        world.client.post("/hr/v1/work-context/refresh", headers=world.headers(nobody)).status_code
        == 403
    )
    # not configured: a clear 503, not a crash
    assert (
        world.client.post("/hr/v1/work-context/refresh", headers=world.headers(hr)).status_code
        == 503
    )
    world.app.state.workcontext = client_for([row()])
    ok = world.client.post("/hr/v1/work-context/refresh", headers=world.headers(hr))
    assert ok.status_code == 200 and ok.json()["assignmentsSeen"] == 1
    too_soon = world.client.post("/hr/v1/work-context/refresh", headers=world.headers(hr))
    assert too_soon.status_code == 409 and too_soon.json()["code"] == "WORK_CONTEXT_TOO_SOON"
    status = world.client.get("/hr/v1/work-context/status", headers=world.headers(hr)).json()
    assert status["lastStatus"] == "OK" and status["assignmentsSeen"] == 1


def test_app_factory_starts_and_stops_the_daily_sync(migrated_engine, monkeypatch):
    """The container starts through app_factory. With Audit Core configured it must come up and
    shut down cleanly: this path crashed the service on DEV once and nothing else exercised it."""
    from fastapi.testclient import TestClient

    from hrmgmt import main as hr_main
    from hrmgmt import workcontext as wc
    from tests.support import settings

    started: list[str] = []
    monkeypatch.setattr(wc.DailySync, "start", lambda self: started.append("start"))
    monkeypatch.setattr(wc.DailySync, "stop", lambda self: started.append("stop"))
    monkeypatch.setattr(hr_main, "get_settings", lambda: settings())
    monkeypatch.setattr(hr_main, "get_engine", lambda: migrated_engine)
    real_create = hr_main.create_app

    def create_with_client():
        app = real_create(settings())
        app.state.workcontext = object()
        return app

    monkeypatch.setattr(hr_main, "create_app", create_with_client)
    app = hr_main.app_factory()
    with TestClient(app) as client:
        assert client.get("/health").json()["service"] == "hrmgmt"
    assert started == ["start", "stop"]
