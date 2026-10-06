from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hrmgmt import permissions as perm
from hrmgmt.authz import AuthzUnavailableError
from hrmgmt.config import Settings
from hrmgmt.main import create_app
from hrmgmt.security import HumanPrincipal, KeysUnavailableError, TokenError


class FakeValidator:
    def validate_human(self, token: str) -> HumanPrincipal:
        if token == "outage":
            raise KeysUnavailableError("down")
        if not token.startswith("user:"):
            raise TokenError("bad")
        return HumanPrincipal(user_id=token.removeprefix("user:"))


class FakeAuthorizer:
    def __init__(self, grants: dict[str, set[str]] | None = None, fail: bool = False):
        self.grants = grants or {}
        self.fail = fail

    def is_allowed(self, *, user_id: str, permission_key: str) -> bool:
        if self.fail:
            raise AuthzUnavailableError("down")
        return permission_key in self.grants.get(user_id, set())


def _settings() -> Settings:
    return Settings(
        environment="TEST",
        database_url="x",
        security_base_url="",
        security_jwks_url="",
        security_issuer="",
        security_audience="",
        security_client_id="",
        security_client_secret="",
        storage_endpoint="",
        storage_bucket="",
        storage_access_key_id="",
        storage_secret_access_key="",
        storage_region="auto",
        allowed_origins=(),
        db_pool_size=2,
        db_max_overflow=0,
    )


@pytest.fixture()
def make_client(migrated_engine):
    def build(grants=None, fail=False) -> TestClient:
        app = create_app(_settings())
        app.state.validator = FakeValidator()
        app.state.authorizer = FakeAuthorizer(grants, fail)
        return TestClient(app, raise_server_exceptions=False)

    return build


def auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer user:{user}"}


def test_health_needs_no_login(make_client):
    assert make_client().get("/health").json() == {"status": "ok", "service": "hrmgmt"}


def test_ready_reports_database(make_client):
    assert make_client().get("/ready").json() == {"status": "ready"}


def test_protected_endpoint_without_token_is_401(make_client):
    r = make_client().get("/hr/v1/me")
    assert r.status_code == 401 and r.json()["code"] == "HR_AUTHENTICATION_FAILED"


def test_bad_token_is_401_and_key_outage_is_503(make_client):
    c = make_client()
    assert c.get("/hr/v1/me", headers={"Authorization": "Bearer junk"}).status_code == 401
    r = c.get("/hr/v1/me", headers={"Authorization": "Bearer outage"})
    assert r.status_code == 503 and r.json()["code"] == "HR_DEPENDENCY_UNAVAILABLE"


def test_me_lists_only_granted_permissions(make_client):
    c = make_client({"hr1": {perm.HR_EMPLOYEE_READ, perm.HR_AUDIT_READ}})
    body = c.get("/hr/v1/me", headers=auth("hr1")).json()
    assert body == {
        "userId": "hr1",
        "permissions": [perm.HR_EMPLOYEE_READ, perm.HR_AUDIT_READ],
        "permissionsComplete": True,
        "employeeId": None,
    }
    assert c.get("/hr/v1/me", headers=auth("nobody")).json()["permissions"] == []


class _Counting(FakeAuthorizer):
    """Records every question asked of Security, and can be slow (unavailable) on chosen permissions."""

    def __init__(self, grants=None, slow=()):
        super().__init__(grants)
        self.asked: list[str] = []
        self.slow = set(slow)

    def is_allowed(self, *, user_id: str, permission_key: str) -> bool:
        self.asked.append(permission_key)
        if permission_key in self.slow:
            raise AuthzUnavailableError("slow")
        return super().is_allowed(user_id=user_id, permission_key=permission_key)


def _me(client, user="emp1"):
    return client.get("/hr/v1/me", headers=auth(user))


def test_me_still_answers_when_security_is_slow_on_a_few_permissions(make_client):
    client = make_client()
    client.app.state.authorizer = _Counting(
        {"hr1": {perm.HR_EMPLOYEE_READ, perm.HR_AUDIT_READ}},
        slow={perm.HR_AUDIT_READ, perm.HR_SETTINGS_MANAGE},
    )
    r = _me(client, "hr1")
    assert r.status_code == 200
    body = r.json()
    assert body["permissions"] == [
        perm.HR_EMPLOYEE_READ
    ]  # the slow one counts as not granted, for now
    assert body["permissionsComplete"] is False
    assert "employeeId" in body


def test_me_still_answers_when_security_is_down_altogether(make_client):
    r = _me(make_client(fail=True))
    assert r.status_code == 200
    assert r.json()["permissions"] == [] and r.json()["permissionsComplete"] is False


def test_me_links_the_employee_even_when_every_check_fails(migrated_engine):
    import uuid

    from sqlalchemy import text

    from tests.support import World

    world = World(migrated_engine)
    hr = world.grant(str(uuid.uuid4()), perm.HR_EMPLOYEE_MANAGE)
    emp, user = world.employee(hr)
    try:
        world.authorizer.fail = True  # Security cannot answer anything
        r = world.client.get("/hr/v1/me", headers=world.headers(user))
        assert r.status_code == 200
        assert r.json()["employeeId"] == emp["employeeId"]
        assert r.json()["permissionsComplete"] is False
    finally:
        # leave nothing behind: other tests list employees a page at a time
        with migrated_engine.begin() as conn:
            for table in ("employee_sensitive", "employee_qualification", "employee_experience"):
                conn.execute(
                    text(f"DELETE FROM hr.{table} WHERE employee_id = CAST(:e AS uuid)"),
                    {"e": emp["employeeId"]},
                )
            conn.execute(
                text("DELETE FROM hr.employee WHERE employee_id = CAST(:e AS uuid)"),
                {"e": emp["employeeId"]},
            )


def test_a_no_is_remembered_briefly_so_opening_the_app_again_does_not_ask_again(
    make_client, monkeypatch
):
    from hrmgmt.api import meta

    client = make_client()
    counting = _Counting({"emp1": set()})
    client.app.state.authorizer = counting
    clock = [1000.0]
    monkeypatch.setattr(meta.time, "monotonic", lambda: clock[0])

    assert _me(client).status_code == 200
    assert len(counting.asked) == len(perm.ALL_PERMISSIONS)  # the first time: one question each
    assert _me(client).status_code == 200
    assert len(counting.asked) == len(perm.ALL_PERMISSIONS)  # again at once: nothing new asked
    clock[0] += meta._DENIED_FOR_SECONDS + 1
    assert _me(client).status_code == 200
    assert len(counting.asked) == 2 * len(perm.ALL_PERMISSIONS)  # after 30 seconds it asks again


def test_only_a_no_is_remembered_a_yes_and_a_slow_answer_are_asked_again(make_client):
    client = make_client()
    counting = _Counting({"emp1": {perm.HR_EMPLOYEE_READ}}, slow={perm.HR_AUDIT_READ})
    client.app.state.authorizer = counting
    _me(client)
    counting.asked.clear()
    _me(client)
    # the granted one and the slow one are asked again; every remembered "no" is not
    assert sorted(counting.asked) == sorted([perm.HR_EMPLOYEE_READ, perm.HR_AUDIT_READ])


def test_a_remembered_no_is_for_the_menu_only_real_actions_still_ask_security(make_client):
    client = make_client({"hr1": set()})
    counting = _Counting({"hr1": set()})
    client.app.state.authorizer = counting
    _me(client, "hr1")  # the menu call remembers a "no" for the audit permission
    counting.grants["hr1"] = {perm.HR_AUDIT_READ}  # then HR is granted it
    # a real action asks Security live and works at once; it is not held back by the menu's memory
    assert client.get("/hr/v1/audit", headers=auth("hr1")).status_code == 200


def test_designations_for_any_signed_in_user(make_client):
    r = make_client().get("/hr/v1/designations", headers=auth("anyone"))
    assert [d["label"] for d in r.json()] == [
        "Analyst",
        "Senior Analyst",
        "Consultant",
        "Senior Consultant",
        "Assistant Manager",
        "Manager",
        "Senior Manager",
        "Director",
        "Partner",
    ]


def test_audit_history_requires_permission(make_client):
    c = make_client({"hr1": {perm.HR_AUDIT_READ}})
    assert c.get("/hr/v1/audit", headers=auth("employee")).status_code == 403
    assert c.get("/hr/v1/audit", headers=auth("hr1")).status_code == 200


def test_permission_check_outage_is_503_not_allow(make_client):
    r = make_client(fail=True).get("/hr/v1/audit", headers=auth("hr1"))
    assert r.status_code == 503


def test_audit_history_filters_and_pages(make_client, migrated_engine):
    from hrmgmt.audit import record_audit

    with migrated_engine.begin() as conn:
        for i in range(3):
            record_audit(
                conn,
                actor_user_id="hr1",
                action="TEST",
                entity_type="paging",
                entity_id=f"e{i}",
                changes={"pan": "changed"},
            )
    c = make_client({"hr1": {perm.HR_AUDIT_READ}})
    page1 = c.get("/hr/v1/audit?entity_type=paging&limit=2", headers=auth("hr1")).json()
    assert len(page1["items"]) == 2 and page1["nextBeforeId"] is not None
    page2 = c.get(
        f"/hr/v1/audit?entity_type=paging&limit=2&before_id={page1['nextBeforeId']}",
        headers=auth("hr1"),
    ).json()
    assert len(page2["items"]) == 1 and page2["nextBeforeId"] is None
    assert page1["items"][0]["changes"] == {"pan": "changed"}


def test_validation_error_never_echoes_submitted_values(make_client):
    c = make_client({"hr1": {perm.HR_AUDIT_READ}})
    r = c.get("/hr/v1/audit?limit=999", headers=auth("hr1"))
    assert r.status_code == 422
    assert r.json()["code"] == "HR_VALIDATION_FAILED"
    assert "999" not in r.text


def test_unconfigured_service_answers_503(migrated_engine):
    app = create_app(_settings())
    r = TestClient(app).get("/hr/v1/me", headers=auth("x"))
    assert r.status_code == 503


class _ShadowAuthorizer(FakeAuthorizer):
    def __init__(self, grants=None, *, batch_answer=None, boom=False):
        super().__init__(grants)
        self.batch_answer = batch_answer
        self.boom = boom
        self.batch_calls: list[dict] = []

    def are_allowed(self, *, user_id, permission_keys, timeout=None, use_remembered=True):
        self.batch_calls.append({"timeout": timeout, "use_remembered": use_remembered})
        if self.boom:
            raise AuthzUnavailableError("slow")
        if self.batch_answer is not None:
            return {k: self.batch_answer(k) for k in permission_keys}
        return {
            k: super(_ShadowAuthorizer, self).is_allowed(user_id=user_id, permission_key=k)
            for k in permission_keys
        }


def test_shadow_check_never_changes_what_me_answers(make_client):
    client = make_client()
    # the batch answer disagrees on purpose, and a second authorizer's batch call blows up
    client.app.state.authorizer = _ShadowAuthorizer(
        {"hr5": {perm.HR_EMPLOYEE_READ}}, batch_answer=lambda key: True
    )
    body = _me(client, "hr5").json()
    assert body["permissions"] == [perm.HR_EMPLOYEE_READ]  # the one-by-one answer, not the batch's
    client.app.state.authorizer = _ShadowAuthorizer({"hr6": {perm.HR_AUDIT_READ}}, boom=True)
    body = _me(client, "hr6").json()
    assert body["permissions"] == [perm.HR_AUDIT_READ]
    assert body["permissionsComplete"] is True


def test_shadow_check_logs_whether_the_batch_agreed_and_how_long_it_took():
    from structlog.testing import capture_logs

    from hrmgmt.api.meta import compare_batch

    perms = (perm.HR_EMPLOYEE_READ, perm.HR_AUDIT_READ)
    same = _ShadowAuthorizer(batch_answer=lambda key: key == perm.HR_EMPLOYEE_READ)
    with capture_logs() as logs:
        compare_batch(same, "u", perms, {perm.HR_EMPLOYEE_READ: True, perm.HR_AUDIT_READ: False})
    row = next(r for r in logs if r["event"] == "hr_batch_shadow")
    assert row["ok"] is True and row["same"] is True and row["different"] == 0
    assert same.batch_calls == [{"timeout": 10.0, "use_remembered": False}]

    with capture_logs() as logs:
        compare_batch(same, "u", perms, {perm.HR_EMPLOYEE_READ: False, perm.HR_AUDIT_READ: None})
    row = next(r for r in logs if r["event"] == "hr_batch_shadow")
    assert row["same"] is False and row["different"] == 1  # an unanswered one is not a difference

    with capture_logs() as logs:
        compare_batch(_ShadowAuthorizer(boom=True), "u", perms, {})
    row = next(r for r in logs if r["event"] == "hr_batch_shadow")
    assert row["ok"] is False and row["error"] == "AuthzUnavailableError"
