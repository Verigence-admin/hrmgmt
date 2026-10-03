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
    assert body == {"userId": "hr1", "permissions": [perm.HR_EMPLOYEE_READ, perm.HR_AUDIT_READ]}
    assert c.get("/hr/v1/me", headers=auth("nobody")).json()["permissions"] == []


def test_designations_for_any_signed_in_user(make_client):
    r = make_client().get("/hr/v1/designations", headers=auth("anyone"))
    assert [d["label"] for d in r.json()] == [
        "Auditor",
        "Senior Auditor",
        "Assistant Manager",
        "Manager",
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
