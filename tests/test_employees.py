from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from hrmgmt import permissions as perm
from hrmgmt.config import Settings
from hrmgmt.main import create_app
from hrmgmt.passwords import generate_initial_password
from hrmgmt.provisioning import CreatedLogin, ProvisioningError, SecurityUserProvisioner
from tests.test_api import FakeAuthorizer, FakeValidator

HR = str(uuid.uuid4())
HR_READER = str(uuid.uuid4())
HR_FULL = str(uuid.uuid4())
NOBODY = str(uuid.uuid4())

GRANTS = {
    HR: {perm.HR_EMPLOYEE_READ, perm.HR_EMPLOYEE_MANAGE},
    HR_READER: {perm.HR_EMPLOYEE_READ},
    HR_FULL: {
        perm.HR_EMPLOYEE_READ,
        perm.HR_EMPLOYEE_MANAGE,
        perm.HR_SENSITIVE_READ,
        perm.HR_AUDIT_READ,
    },
}


class FakeProvisioner:
    def __init__(self, error: str | None = None):
        self.error = error
        self.calls: list[dict] = []

    def create_user(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise ProvisioningError(self.error, "x")
        return CreatedLogin(user_id=str(uuid.uuid4()))


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
        allowed_origins=(),
        db_pool_size=2,
        db_max_overflow=0,
    )


@pytest.fixture()
def make_client(migrated_engine):
    def build(provisioner="default") -> tuple[TestClient, FakeProvisioner | None]:
        app = create_app(_settings())
        app.state.validator = FakeValidator()
        app.state.authorizer = FakeAuthorizer(GRANTS)
        prov = FakeProvisioner() if provisioner == "default" else provisioner
        app.state.provisioner = prov
        return TestClient(app, raise_server_exceptions=False), prov

    return build


def auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer user:{user}"}


def payload(**over):
    n = uuid.uuid4().hex[:8]
    body = {
        "employee_code": f"T{n}".upper(),
        "full_name": "Asha Rao",
        "personal_email": f"asha.{n}@example.com",
        "mobile": "98765 43210",
        "pan": "ABCDE1234F",
        "aadhaar": "1234 5678 9012".replace(" ", ""),
    }
    body.update(over)
    return body


def audit_rows(engine, entity_id):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT action, changes::text FROM hr.audit_log WHERE entity_id = :e ORDER BY audit_id"
            ),
            {"e": entity_id},
        ).all()


# ---- creation and login -------------------------------------------------------------------


def test_create_makes_login_and_shows_password_once(make_client, migrated_engine):
    client, prov = make_client()
    r = client.post("/hr/v1/employees", json=payload(), headers=auth(HR))
    assert r.status_code == 201
    body = r.json()
    emp = body["employee"]
    assert emp["loginStatus"] == "CREATED" and emp["loginErrorCode"] is None
    password = body["initialPassword"]
    assert len(password) >= 12
    call = prov.calls[0]
    assert call["mobile"] == "9876543210" and call["email"] == emp["personalEmail"]
    assert (
        call["first_name"] == "Asha" and call["last_name"] == "Rao" and call["password"] == password
    )
    # the password is shown once: not on a later read, not in the audit log
    again = client.get(f"/hr/v1/employees/{emp['employeeId']}", headers=auth(HR))
    assert password not in again.text
    for _, changes in audit_rows(migrated_engine, emp["employeeId"]):
        assert password not in changes
    actions = [a for a, _ in audit_rows(migrated_engine, emp["employeeId"])]
    assert actions == ["EMPLOYEE_CREATED", "LOGIN_CREATED"]


def test_login_failure_keeps_the_employee(make_client):
    client, _ = make_client(FakeProvisioner(error="EMAIL_OR_MOBILE_EXISTS"))
    r = client.post("/hr/v1/employees", json=payload(), headers=auth(HR))
    assert r.status_code == 201
    emp = r.json()["employee"]
    assert emp["loginStatus"] == "FAILED" and emp["loginErrorCode"] == "EMAIL_OR_MOBILE_EXISTS"
    assert "initialPassword" not in r.json()


def test_no_mobile_means_no_login_attempt(make_client):
    client, prov = make_client()
    r = client.post("/hr/v1/employees", json=payload(mobile=None), headers=auth(HR))
    emp = r.json()["employee"]
    assert emp["loginStatus"] == "FAILED" and emp["loginErrorCode"] == "CONTACT_NOT_VALID"
    assert prov.calls == [] and "MOBILE_MISSING" in emp["dataFlags"]


def test_unconfigured_security_marks_login_failed(make_client):
    client, _ = make_client(provisioner=None)
    emp = client.post("/hr/v1/employees", json=payload(), headers=auth(HR)).json()["employee"]
    assert emp["loginStatus"] == "FAILED" and emp["loginErrorCode"] == "NOT_CONFIGURED"


def test_create_without_login_and_retry_later(make_client):
    client, prov = make_client()
    emp = client.post(
        "/hr/v1/employees", json=payload(create_login=False), headers=auth(HR)
    ).json()["employee"]
    assert emp["loginStatus"] == "NOT_CREATED" and prov.calls == []
    r = client.post(f"/hr/v1/employees/{emp['employeeId']}/login", headers=auth(HR))
    assert r.status_code == 200 and r.json()["employee"]["loginStatus"] == "CREATED"
    assert "initialPassword" in r.json()
    again = client.post(f"/hr/v1/employees/{emp['employeeId']}/login", headers=auth(HR))
    assert again.status_code == 409 and again.json()["code"] == "LOGIN_ALREADY_CREATED"
    assert len(prov.calls) == 1


def test_duplicate_code_and_email_are_refused_cleanly(make_client):
    client, _ = make_client()
    first = payload()
    assert client.post("/hr/v1/employees", json=first, headers=auth(HR)).status_code == 201
    same_code = payload(employee_code=first["employee_code"].lower())
    r = client.post("/hr/v1/employees", json=same_code, headers=auth(HR))
    assert r.status_code == 409 and r.json()["code"] == "EMPLOYEE_CODE_EXISTS"
    same_email = payload(personal_email=first["personal_email"].upper())
    r = client.post("/hr/v1/employees", json=same_email, headers=auth(HR))
    assert r.status_code == 409 and r.json()["code"] == "EMPLOYEE_EMAIL_EXISTS"


# ---- data quality: create anyway, flag it -------------------------------------------------


def test_duplicate_and_missing_pan_are_flagged_not_rejected(make_client):
    client, _ = make_client()
    pan = "QWERT4321Z"
    a = client.post("/hr/v1/employees", json=payload(pan=pan), headers=auth(HR)).json()["employee"]
    b = client.post("/hr/v1/employees", json=payload(pan=pan), headers=auth(HR))
    assert b.status_code == 201
    b = b.json()["employee"]
    c = client.post(
        "/hr/v1/employees", json=payload(pan=None, aadhaar=None), headers=auth(HR)
    ).json()["employee"]
    got_a = client.get(f"/hr/v1/employees/{a['employeeId']}", headers=auth(HR)).json()
    assert "PAN_DUPLICATE" in got_a["dataFlags"] and "PAN_DUPLICATE" in b["dataFlags"]
    assert "PAN_MISSING" in c["dataFlags"] and "AADHAAR_MISSING" in c["dataFlags"]


def test_invalid_pan_or_aadhaar_is_a_validation_error_that_does_not_echo(make_client):
    client, _ = make_client()
    r = client.post("/hr/v1/employees", json=payload(pan="NOT-A-PAN"), headers=auth(HR))
    assert r.status_code == 422 and "NOT-A-PAN" not in r.text
    r = client.post("/hr/v1/employees", json=payload(aadhaar="12345"), headers=auth(HR))
    assert r.status_code == 422 and "12345" not in r.text


def test_unknown_fields_are_refused(make_client):
    client, _ = make_client()
    r = client.post(
        "/hr/v1/employees", json=payload(security_user_id=str(uuid.uuid4())), headers=auth(HR)
    )
    assert r.status_code == 422


# ---- protected numbers ---------------------------------------------------------------------


def test_protected_numbers_are_masked_everywhere_except_the_reveal(make_client, migrated_engine):
    client, _ = make_client()
    emp = client.post("/hr/v1/employees", json=payload(), headers=auth(HR)).json()["employee"]
    assert emp["panMasked"] == "XXXXX1234F" and emp["aadhaarMasked"] == "XXXX XXXX 9012"
    listing = client.get("/hr/v1/employees?q=" + emp["employeeCode"], headers=auth(HR))
    detail = client.get(f"/hr/v1/employees/{emp['employeeId']}", headers=auth(HR))
    for response in (listing, detail):
        assert "ABCDE1234F" not in response.text and "123456789012" not in response.text
    # reveal needs its own permission
    url = f"/hr/v1/employees/{emp['employeeId']}/sensitive"
    assert client.get(url, headers=auth(HR)).status_code == 403
    full = client.get(url, headers=auth(HR_FULL))
    assert full.json() == {"pan": "ABCDE1234F", "aadhaar": "123456789012"}
    rows = audit_rows(migrated_engine, emp["employeeId"])
    assert rows[-1][0] == "SENSITIVE_REVEALED"
    assert all("ABCDE1234F" not in c and "123456789012" not in c for _, c in rows)


# ---- permissions ---------------------------------------------------------------------------


def test_permissions_are_enforced_on_the_server(make_client):
    client, _ = make_client()
    emp = client.post("/hr/v1/employees", json=payload(), headers=auth(HR)).json()["employee"]
    eid = emp["employeeId"]
    assert client.post("/hr/v1/employees", json=payload(), headers=auth(NOBODY)).status_code == 403
    assert (
        client.post("/hr/v1/employees", json=payload(), headers=auth(HR_READER)).status_code == 403
    )
    assert client.get("/hr/v1/employees", headers=auth(NOBODY)).status_code == 403
    assert client.get(f"/hr/v1/employees/{eid}", headers=auth(NOBODY)).status_code == 403
    assert client.get(f"/hr/v1/employees/{eid}", headers=auth(HR_READER)).status_code == 200
    assert (
        client.patch(
            f"/hr/v1/employees/{eid}", json={"department": "X"}, headers=auth(HR_READER)
        ).status_code
        == 403
    )
    assert client.post(f"/hr/v1/employees/{eid}/login", headers=auth(HR_READER)).status_code == 403


def test_unknown_employee_is_404_not_500(make_client):
    client, _ = make_client()
    assert client.get(f"/hr/v1/employees/{uuid.uuid4()}", headers=auth(HR)).status_code == 404
    assert client.get("/hr/v1/employees/not-a-uuid", headers=auth(HR)).status_code == 404


# ---- HR updates ----------------------------------------------------------------------------


def test_hr_update_records_old_and_new_but_never_protected_values(make_client, migrated_engine):
    client, _ = make_client()
    emp = client.post("/hr/v1/employees", json=payload(), headers=auth(HR)).json()["employee"]
    eid = emp["employeeId"]
    r = client.patch(
        f"/hr/v1/employees/{eid}",
        json={"designation_code": "SENIOR_AUDITOR", "department": "  RM ", "pan": "ZZZZZ9999Z"},
        headers=auth(HR),
    )
    assert r.status_code == 200
    got = r.json()
    assert got["designation"] == "Senior Auditor" and got["department"] == "RM"
    assert got["panMasked"] == "XXXXX9999Z"
    last = audit_rows(migrated_engine, eid)[-1]
    changes = json.loads(last[1])
    assert last[0] == "EMPLOYEE_UPDATED"
    assert changes["designation_code"] == {"from": None, "to": "SENIOR_AUDITOR"}
    assert (
        changes["pan"] == "changed" and "ZZZZZ9999Z" not in last[1] and "ABCDE1234F" not in last[1]
    )


def test_update_rejects_unknown_designation_and_unknown_fields(make_client):
    client, _ = make_client()
    eid = client.post("/hr/v1/employees", json=payload(), headers=auth(HR)).json()["employee"][
        "employeeId"
    ]
    r = client.patch(f"/hr/v1/employees/{eid}", json={"designation_code": "CEO"}, headers=auth(HR))
    assert r.status_code == 409 and r.json()["code"] == "DESIGNATION_UNKNOWN"
    r = client.patch(
        f"/hr/v1/employees/{eid}", json={"security_user_id": str(uuid.uuid4())}, headers=auth(HR)
    )
    assert r.status_code == 422


def test_update_with_no_real_change_writes_no_audit_row(make_client, migrated_engine):
    client, _ = make_client()
    emp = client.post("/hr/v1/employees", json=payload(), headers=auth(HR)).json()["employee"]
    before = len(audit_rows(migrated_engine, emp["employeeId"]))
    client.patch(
        f"/hr/v1/employees/{emp['employeeId']}",
        json={"department": emp["department"]},
        headers=auth(HR),
    )
    assert len(audit_rows(migrated_engine, emp["employeeId"])) == before


# ---- self service --------------------------------------------------------------------------


def _linked_employee(client, migrated_engine, **over):
    emp = client.post("/hr/v1/employees", json=payload(**over), headers=auth(HR)).json()["employee"]
    with migrated_engine.connect() as conn:
        user_id = str(
            conn.execute(
                text(
                    "SELECT security_user_id FROM hr.employee WHERE employee_id = CAST(:i AS uuid)"
                ),
                {"i": emp["employeeId"]},
            ).scalar_one()
        )
    return emp, user_id


def test_employee_reads_and_edits_only_their_own_permitted_fields(make_client, migrated_engine):
    client, _ = make_client()
    emp, me = _linked_employee(client, migrated_engine)
    other, _ = _linked_employee(client, migrated_engine)
    mine = client.get("/hr/v1/me/employee", headers=auth(me))
    assert mine.status_code == 200 and mine.json()["employeeId"] == emp["employeeId"]
    r = client.patch(
        "/hr/v1/me/employee",
        json={
            "address": "12 New Street",
            "emergency_contact_name": "Ravi",
            "emergency_contact_number": "9123456789",
            "secondary_email": "Backup@Example.com",
        },
        headers=auth(me),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["address"] == "12 New Street" and body["secondaryEmail"] == "backup@example.com"
    assert body["emergencyContactNumber"] == "9123456789"
    # the other employee is untouched
    assert (
        client.get(f"/hr/v1/employees/{other['employeeId']}", headers=auth(HR)).json()["address"]
        != "12 New Street"
    )


@pytest.mark.parametrize(
    "forbidden",
    [
        {"full_name": "Hacker"},
        {"designation_code": "MANAGER"},
        {"pan": "ABCDE1234F"},
        {"employment_status": "ACTIVE"},
        {"department": "X"},
        {"employee_id": str(uuid.uuid4())},
        {"personal_email": "x@y.com"},
    ],
)
def test_employee_cannot_change_hr_fields(make_client, migrated_engine, forbidden):
    client, _ = make_client()
    _, me = _linked_employee(client, migrated_engine)
    assert client.patch("/hr/v1/me/employee", json=forbidden, headers=auth(me)).status_code == 422


def test_user_without_employee_record_gets_404_and_exited_employee_loses_access(
    make_client, migrated_engine
):
    client, _ = make_client()
    assert client.get("/hr/v1/me/employee", headers=auth(NOBODY)).status_code == 404
    emp, me = _linked_employee(client, migrated_engine)
    client.patch(
        f"/hr/v1/employees/{emp['employeeId']}",
        json={"employment_status": "EXITED"},
        headers=auth(HR),
    )
    assert client.get("/hr/v1/me/employee", headers=auth(me)).status_code == 404


def test_employee_can_reveal_only_their_own_numbers_and_it_is_audited(make_client, migrated_engine):
    client, _ = make_client()
    emp, me = _linked_employee(client, migrated_engine)
    r = client.get("/hr/v1/me/employee/sensitive", headers=auth(me))
    assert r.json() == {"pan": "ABCDE1234F", "aadhaar": "123456789012"}
    assert audit_rows(migrated_engine, emp["employeeId"])[-1][0] == "SENSITIVE_REVEALED_SELF"


# ---- password generator and Security contract ----------------------------------------------


def test_generated_passwords_are_strong_and_different():
    seen = {generate_initial_password() for _ in range(50)}
    assert len(seen) == 50
    for p in seen:
        assert len(p) == 16
        assert any(c.isupper() for c in p) and any(c.islower() for c in p)
        assert any(c.isdigit() for c in p) and any(c in "@#%+=!" for c in p)
        assert not set(p) & set("0O1lI")
    with pytest.raises(ValueError):
        generate_initial_password(8)


def _provisioner(status: int, body: dict | None = None):
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(status, json=body or {})

    prov = SecurityUserProvisioner(
        base_url="https://security.invalid",
        token_provider=lambda: "svc",
        transport=httpx.MockTransport(handler),
    )
    return prov, sent


def test_provisioner_sends_the_agreed_request_once():
    prov, sent = _provisioner(201, {"userId": "u-1"})
    got = prov.create_user(
        first_name="A", last_name="B", email="a@b.co", mobile="9876543210", password="pw"
    )
    assert got.user_id == "u-1" and len(sent) == 1
    req = sent[0]
    assert (
        req.url.path == "/security/v1/service/users"
        and req.headers["authorization"] == "Bearer svc"
    )
    assert json.loads(req.content) == {
        "firstName": "A",
        "lastName": "B",
        "email": "a@b.co",
        "mobile": "9876543210",
        "password": "pw",
    }


@pytest.mark.parametrize(
    "status,code",
    [
        (409, "EMAIL_OR_MOBILE_EXISTS"),
        (422, "CONTACT_NOT_VALID"),
        (403, "NOT_PERMITTED"),
        (500, "SECURITY_UNAVAILABLE"),
        (503, "SECURITY_UNAVAILABLE"),
    ],
)
def test_provisioner_maps_failures_and_never_retries(status, code):
    prov, sent = _provisioner(status)
    with pytest.raises(ProvisioningError) as exc:
        prov.create_user(
            first_name="A", last_name="B", email="a@b.co", mobile="9876543210", password="pw"
        )
    assert exc.value.code == code and len(sent) == 1
    assert "pw" not in str(exc.value)
