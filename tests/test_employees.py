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
from hrmgmt.provisioning import (
    CreatedLogin,
    ProvisioningError,
    SecurityUserProvisioner,
    SyncOutcome,
)
from hrmgmt.storage import StorageError
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


class FakeStorage:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(self, key, data, content_type):
        if self.fail:
            raise StorageError("down")
        self.objects[key] = (data, content_type)

    def get(self, key):
        if self.fail:
            raise StorageError("down")
        return self.objects[key][0]


class FakeProvisioner:
    def __init__(self, error: str | None = None):
        self.error = error
        self.calls: list[dict] = []
        self.existing: dict = {}

    def create_user(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise ProvisioningError(self.error, "x")
        return CreatedLogin(user_id=str(uuid.uuid4()))

    def find_user(self, *, email):
        return self.existing.get(email)

    def mark_employee(self, *, user_id):
        self.marked = getattr(self, "marked", []) + [user_id]

    def list_users(self, *, q=None, ids=None, limit=100, offset=0):
        users = list(getattr(self, "users", []))
        if ids:
            users = [u for u in users if u.user_id in ids]
        if q:
            users = [u for u in users if q.lower() in (u.display_name or "").lower()]
        return users[offset : offset + limit]

    def sync_employees(self, *, items):
        import dataclasses

        self.synced = getattr(self, "synced", []) + [list(items)]
        users = {u.user_id: u for u in getattr(self, "users", [])}
        out = []
        for uid, suspend in items:
            u = users.get(uid)
            if u is None:
                out.append(SyncOutcome(uid, False, None, False, False, "NOT_FOUND"))
                continue
            do_suspend = suspend and u.status == "ACTIVE"
            note = None if (do_suspend or not suspend) else "NOT_ACTIVE"
            status = "SUSPENDED" if do_suspend else u.status
            out.append(SyncOutcome(uid, True, status, not u.is_employee, do_suspend, note))
            users[uid] = dataclasses.replace(u, status=status, is_employee=True)
        self.users = list(users.values())
        return out

    def set_password(self, *, user_id, password):
        self.passwords = getattr(self, "passwords", []) + [(user_id, password)]
        if getattr(self, "set_password_error", None):
            raise ProvisioningError(self.set_password_error, "x")
        return f"login-{user_id[:8]}@example.com"


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
    def build(
        provisioner="default", storage="default"
    ) -> tuple[TestClient, FakeProvisioner | None]:
        app = create_app(_settings())
        app.state.validator = FakeValidator()
        app.state.authorizer = FakeAuthorizer(GRANTS)
        prov = FakeProvisioner() if provisioner == "default" else provisioner
        app.state.provisioner = prov
        app.state.storage = FakeStorage() if storage == "default" else storage
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


def test_me_reports_the_linked_employee_record(make_client, migrated_engine):
    client, _ = make_client()
    emp, me = _linked_employee(client, migrated_engine)
    body = client.get("/hr/v1/me", headers=auth(me)).json()
    assert body["employeeId"] == emp["employeeId"]
    assert client.get("/hr/v1/me", headers=auth(NOBODY)).json()["employeeId"] is None


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


# ---- more onboarding fields and qualifications -------------------------------------------


def full_profile(**over):
    return payload(
        state="odisha",
        pincode="751001",
        total_experience_years=3.5,
        emergency_contact_name="Ravi Rao",
        emergency_contact_number="+91 91234 56789",
        emergency_contact_address="12 Station Road, Cuttack",
        qualifications=[
            {"degree_code": "BCOM", "percentage": 72.5, "year_of_passing": 2020},
            {"degree_code": "MBA", "percentage": 68, "year_of_passing": 2022},
        ],
        **over,
    )


def test_create_with_full_profile_and_two_qualifications(make_client):
    client, _ = make_client()
    r = client.post("/hr/v1/employees", json=full_profile(), headers=auth(HR))
    assert r.status_code == 201
    emp = r.json()["employee"]
    assert emp["state"] == "Odisha" and emp["pincode"] == "751001"
    assert emp["totalExperienceYears"] == 3.5
    assert emp["emergencyContactNumber"] == "9123456789"
    assert emp["emergencyContactAddress"] == "12 Station Road, Cuttack"
    quals = emp["qualifications"]
    assert [q["degreeCode"] for q in quals] == ["MBA", "BCOM"]  # newest first
    assert quals[0]["level"] == "MASTER" and quals[0]["percentage"] == 68.0
    assert (
        quals[1]["degree"] == "Bachelor of Commerce (B.Com.)" and quals[1]["yearOfPassing"] == 2020
    )


@pytest.mark.parametrize(
    "over",
    [
        {"state": "Atlantis"},
        {"pincode": "012345"},
        {"pincode": "7510"},
        {"total_experience_years": -1},
        {"total_experience_years": 61},
        {"emergency_contact_number": "12345"},
        {"qualifications": [{"degree_code": "BCOM", "percentage": 101, "year_of_passing": 2020}]},
        {"qualifications": [{"degree_code": "BCOM", "percentage": 50, "year_of_passing": 1900}]},
        {"qualifications": [{"degree_code": "BCOM", "percentage": 50, "year_of_passing": 2999}]},
        {"qualifications": [{"degree_code": "OTHER", "percentage": 50, "year_of_passing": 2020}]},
    ],
)
def test_invalid_profile_values_are_refused(make_client, over):
    client, _ = make_client()
    r = client.post("/hr/v1/employees", json=payload(**over), headers=auth(HR))
    assert r.status_code == 422 and r.json()["code"] == "HR_VALIDATION_FAILED"


def test_unknown_degree_rolls_back_the_whole_employee(make_client, migrated_engine):
    client, _ = make_client()
    body = payload(
        qualifications=[{"degree_code": "NOPE", "percentage": 50, "year_of_passing": 2020}]
    )
    r = client.post("/hr/v1/employees", json=body, headers=auth(HR))
    assert r.status_code == 409 and r.json()["code"] == "DEGREE_UNKNOWN"
    with migrated_engine.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM hr.employee WHERE employee_code = :c"),
            {"c": body["employee_code"]},
        ).scalar_one()
    assert n == 0


def test_other_degree_is_recorded_by_name(make_client):
    client, _ = make_client()
    body = payload(
        qualifications=[
            {
                "degree_code": "OTHER",
                "degree_other": " Diploma in  Surveying ",
                "percentage": 80,
                "year_of_passing": 2019,
            }
        ]
    )
    emp = client.post("/hr/v1/employees", json=body, headers=auth(HR)).json()["employee"]
    assert emp["qualifications"][0]["degree"] == "Diploma in Surveying"


def test_catalogues_cover_bachelors_and_masters_and_all_states(make_client):
    client, _ = make_client()
    degrees = client.get("/hr/v1/degrees", headers=auth(NOBODY)).json()
    levels = {d["level"] for d in degrees}
    codes = {d["code"] for d in degrees}
    assert levels == {"BACHELOR", "MASTER", "OTHER"}
    assert {"BA", "BSC", "BCOM", "BBA", "BCA", "BTECH", "BE", "LLB", "MBBS", "BPHARM"} <= codes
    assert {"MA", "MSC", "MCOM", "MBA", "MCA", "MTECH", "LLM", "MD", "MPHARM"} <= codes
    assert len(codes) == len(degrees)
    states = client.get("/hr/v1/states", headers=auth(NOBODY)).json()
    assert len(states) == 36 and "Odisha" in states and "Delhi" in states
    assert client.get("/hr/v1/degrees").status_code == 401


def test_qualification_add_replace_remove_with_audit(make_client, migrated_engine):
    client, _ = make_client()
    eid = client.post("/hr/v1/employees", json=payload(), headers=auth(HR)).json()["employee"][
        "employeeId"
    ]
    base = f"/hr/v1/employees/{eid}/qualifications"
    added = client.post(
        base,
        json={"degree_code": "BTECH", "percentage": 81.25, "year_of_passing": 2021},
        headers=auth(HR),
    )
    assert added.status_code == 201
    qid = added.json()["qualifications"][0]["qualificationId"]
    changed = client.put(
        f"{base}/{qid}",
        json={"degree_code": "BE", "percentage": 82, "year_of_passing": 2021},
        headers=auth(HR),
    )
    assert changed.json()["qualifications"][0]["degreeCode"] == "BE"
    assert (
        client.put(
            f"{base}/{uuid.uuid4()}",
            json={"degree_code": "BE", "percentage": 82, "year_of_passing": 2021},
            headers=auth(HR),
        ).status_code
        == 404
    )
    gone = client.delete(f"{base}/{qid}", headers=auth(HR))
    assert gone.json()["qualifications"] == []
    assert client.delete(f"{base}/{qid}", headers=auth(HR)).status_code == 404
    actions = [a for a, _ in audit_rows(migrated_engine, eid)]
    assert actions[-3:] == ["QUALIFICATION_ADDED", "QUALIFICATION_UPDATED", "QUALIFICATION_REMOVED"]
    assert (
        client.post(
            base,
            json={"degree_code": "BE", "percentage": 50, "year_of_passing": 2021},
            headers=auth(HR_READER),
        ).status_code
        == 403
    )


def test_qualification_of_another_employee_cannot_be_touched_through_a_wrong_id(make_client):
    client, _ = make_client()
    a = client.post("/hr/v1/employees", json=full_profile(), headers=auth(HR)).json()["employee"]
    b = client.post("/hr/v1/employees", json=payload(), headers=auth(HR)).json()["employee"]
    qid = a["qualifications"][0]["qualificationId"]
    r = client.delete(f"/hr/v1/employees/{b['employeeId']}/qualifications/{qid}", headers=auth(HR))
    assert r.status_code == 404
    still = client.get(f"/hr/v1/employees/{a['employeeId']}", headers=auth(HR)).json()
    assert len(still["qualifications"]) == 2


def test_employee_edits_own_state_pincode_and_emergency_address_but_not_qualifications(
    make_client, migrated_engine
):
    client, _ = make_client()
    emp, me = _linked_employee(client, migrated_engine)
    r = client.patch(
        "/hr/v1/me/employee",
        json={
            "state": "West Bengal",
            "pincode": "700001",
            "emergency_contact_address": "5 Park St",
        },
        headers=auth(me),
    )
    assert r.status_code == 200
    assert r.json()["state"] == "West Bengal" and r.json()["emergencyContactAddress"] == "5 Park St"
    for forbidden in ({"total_experience_years": 20}, {"qualifications": []}):
        assert (
            client.patch("/hr/v1/me/employee", json=forbidden, headers=auth(me)).status_code == 422
        )
    assert (
        client.post(
            f"/hr/v1/employees/{emp['employeeId']}/qualifications",
            json={"degree_code": "BE", "percentage": 50, "year_of_passing": 2021},
            headers=auth(me),
        ).status_code
        == 403
    )  # the HR route stays closed to an employee; their own routes are /me/employee/qualifications


def test_employee_manages_own_qualifications_but_cannot_change_email(make_client, migrated_engine):
    client, _ = make_client()
    emp, me = _linked_employee(client, migrated_engine)
    q = {
        "degree_code": "BCOM",
        "percentage": 70,
        "year_of_passing": 2020,
        "university": "Utkal University",
        "college": "Ravenshaw College",
    }
    r = client.post("/hr/v1/me/employee/qualifications", json=q, headers=auth(me))
    assert r.status_code == 201
    quals = r.json()["qualifications"]
    assert quals[0]["university"] == "Utkal University"
    qid = quals[0]["qualificationId"]
    r = client.put(
        f"/hr/v1/me/employee/qualifications/{qid}",
        json={**q, "percentage": 75},
        headers=auth(me),
    )
    assert r.status_code == 200 and r.json()["qualifications"][0]["percentage"] == 75.0
    # a bad degree and an id that is not theirs are refused
    bad = client.post(
        "/hr/v1/me/employee/qualifications", json={**q, "degree_code": "NOPE"}, headers=auth(me)
    )
    assert bad.status_code == 409
    other, other_user = _linked_employee(client, migrated_engine)
    other_q = client.post(
        "/hr/v1/me/employee/qualifications", json=q, headers=auth(other_user)
    ).json()["qualifications"][0]["qualificationId"]
    assert (
        client.put(
            f"/hr/v1/me/employee/qualifications/{other_q}", json=q, headers=auth(me)
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/hr/v1/me/employee/qualifications/{other_q}", headers=auth(me)).status_code
        == 404
    )
    assert (
        client.delete(f"/hr/v1/me/employee/qualifications/{qid}", headers=auth(me)).status_code
        == 200
    )
    # the login email cannot be changed by the employee
    for body in ({"personal_email": "new@example.com"}, {"mobile": "9000000000"}):
        assert client.patch("/hr/v1/me/employee", json=body, headers=auth(me)).status_code == 422
    assert (
        client.get("/hr/v1/me/employee", headers=auth(me)).json()["personalEmail"]
        == emp["personalEmail"]
    )


# ---- profile photo ---------------------------------------------------------------------------


def _jpeg_with_gps(size=(1600, 1200)) -> bytes:
    import io

    from PIL import Image

    image = Image.new("RGB", size, (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "SecretCameraMake"
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (20.0, 17.0, 0.0)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def test_photo_is_resized_stripped_of_metadata_and_served_back(make_client, migrated_engine):
    import io

    from PIL import Image

    storage = FakeStorage()
    client, _ = make_client(storage=storage)
    emp, me = _linked_employee(client, migrated_engine)
    raw = _jpeg_with_gps()
    assert b"SecretCameraMake" in raw
    r = client.post(
        "/hr/v1/me/employee/photo", files={"file": ("me.jpg", raw, "image/jpeg")}, headers=auth(me)
    )
    assert r.status_code == 200 and r.json()["hasPhoto"] is True
    stored, content_type = storage.objects[f"employee/{emp['employeeId']}/photo.jpg"]
    assert content_type == "image/jpeg" and b"SecretCameraMake" not in stored
    image = Image.open(io.BytesIO(stored))
    assert max(image.size) == 512 and not image.getexif()
    mine = client.get("/hr/v1/me/employee/photo", headers=auth(me))
    assert mine.status_code == 200 and mine.content == stored
    assert mine.headers["cache-control"] == "private, no-store"
    assert (
        client.get(
            f"/hr/v1/employees/{emp['employeeId']}/photo", headers=auth(HR_READER)
        ).status_code
        == 200
    )
    assert (
        client.get(f"/hr/v1/employees/{emp['employeeId']}/photo", headers=auth(NOBODY)).status_code
        == 403
    )
    assert audit_rows(migrated_engine, emp["employeeId"])[-1][0] == "PHOTO_CHANGED"


def test_photo_is_optional_and_missing_photo_is_a_clean_404(make_client, migrated_engine):
    client, _ = make_client()
    emp, me = _linked_employee(client, migrated_engine)
    assert emp["hasPhoto"] is False
    assert client.get("/hr/v1/me/employee/photo", headers=auth(me)).status_code == 404


@pytest.mark.parametrize(
    "name,data",
    [
        ("a.txt", b"hello"),
        ("a.jpg", b""),
        ("a.jpg", b"\xff\xd8\xff not really"),
        ("a.pdf", b"%PDF-1.4 x"),
    ],
)
def test_non_images_are_refused_without_touching_the_record(
    make_client, migrated_engine, name, data
):
    storage = FakeStorage()
    client, _ = make_client(storage=storage)
    _, me = _linked_employee(client, migrated_engine)
    r = client.post(
        "/hr/v1/me/employee/photo", files={"file": (name, data, "image/jpeg")}, headers=auth(me)
    )
    assert r.status_code == 422 and r.json()["code"] == "HR_PHOTO_NOT_ACCEPTED"
    assert storage.objects == {}
    assert client.get("/hr/v1/me/employee", headers=auth(me)).json()["hasPhoto"] is False


def test_oversize_photo_is_refused(make_client, migrated_engine):
    client, _ = make_client()
    _, me = _linked_employee(client, migrated_engine)
    big = b"\xff\xd8\xff" + b"0" * (5 * 1024 * 1024 + 10)
    r = client.post(
        "/hr/v1/me/employee/photo", files={"file": ("a.jpg", big, "image/jpeg")}, headers=auth(me)
    )
    assert r.status_code == 422 and "5 MB" in r.json()["detail"]


def test_storage_outage_or_missing_storage_is_503_and_leaves_no_photo_flag(
    make_client, migrated_engine
):
    for storage in (FakeStorage(fail=True), None):
        client, _ = make_client(storage=storage)
        _, me = _linked_employee(client, migrated_engine)
        r = client.post(
            "/hr/v1/me/employee/photo",
            files={"file": ("a.jpg", _jpeg_with_gps((50, 50)), "image/jpeg")},
            headers=auth(me),
        )
        assert r.status_code == 503
        assert client.get("/hr/v1/me/employee", headers=auth(me)).json()["hasPhoto"] is False


def test_hr_can_set_a_photo_for_an_employee_but_a_reader_cannot(make_client):
    storage = FakeStorage()
    client, _ = make_client(storage=storage)
    eid = client.post("/hr/v1/employees", json=payload(), headers=auth(HR)).json()["employee"][
        "employeeId"
    ]
    jpg = _jpeg_with_gps((60, 40))
    assert (
        client.post(
            f"/hr/v1/employees/{eid}/photo",
            files={"file": ("p.jpg", jpg, "image/jpeg")},
            headers=auth(HR_READER),
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/hr/v1/employees/{eid}/photo",
            files={"file": ("p.jpg", jpg, "image/jpeg")},
            headers=auth(HR),
        ).status_code
        == 200
    )
    assert f"employee/{eid}/photo.jpg" in storage.objects


# ---- linking an existing login ----------------------------------------------------------------


def _link(client, employee_id, body=None, user=HR):
    return client.post(
        f"/hr/v1/employees/{employee_id}/link-login", json=body or {}, headers=auth(user)
    )


def _made(client, **over):
    r = client.post("/hr/v1/employees", json=payload(**over), headers=auth(HR))
    assert r.status_code == 201, r.text
    return r.json()["employee"]


def test_link_an_existing_login_by_the_employee_email(make_client):
    from hrmgmt.provisioning import FoundLogin

    client, prov = make_client(provisioner=None)
    # created without a login service, so no login exists yet
    emp = _made(client)
    assert emp["loginStatus"] == "FAILED"
    client.app.state.provisioner = fake = FakeProvisioner()
    existing = str(uuid.uuid4())
    fake.existing[emp["personalEmail"]] = FoundLogin(existing, "Link Person", "ACTIVE")
    r = _link(client, emp["employeeId"])
    assert r.status_code == 200 and r.json()["employee"]["loginStatus"] == "CREATED"
    assert fake.marked == [existing]  # Security was told to tick "Is Employee"
    again = _link(client, emp["employeeId"])
    assert again.status_code == 409 and again.json()["code"] == "LOGIN_ALREADY_LINKED"


def test_link_refuses_unknown_inactive_and_already_used_logins(make_client):
    from hrmgmt.provisioning import FoundLogin

    client, prov = make_client(provisioner=None)
    first, second = _made(client), _made(client)
    client.app.state.provisioner = fake = FakeProvisioner()
    missing = _link(client, first["employeeId"])
    assert missing.status_code == 404 and missing.json()["code"] == "LOGIN_NOT_FOUND"
    fake.existing["off@example.com"] = FoundLogin(str(uuid.uuid4()), None, "SUSPENDED")
    inactive = _link(client, first["employeeId"], {"email": "off@example.com"})
    assert inactive.status_code == 409 and inactive.json()["code"] == "LOGIN_NOT_ACTIVE"
    shared = FoundLogin(str(uuid.uuid4()), "Shared", "PENDING")  # waiting for SuperAdmin
    fake.existing["shared@example.com"] = shared
    assert _link(client, first["employeeId"], {"email": "shared@example.com"}).status_code == 200
    taken = _link(client, second["employeeId"], {"email": "shared@example.com"})
    assert taken.status_code == 409 and taken.json()["code"] == "LOGIN_IN_USE"


def test_link_needs_the_manage_permission(make_client):
    client, _ = make_client()
    emp = _made(client)
    assert _link(client, emp["employeeId"], user="nobody").status_code == 403


def test_provisioner_lookup_maps_answers_and_never_retries():
    prov, sent = _provisioner(200, {"userId": "u-9", "displayName": "Pat", "status": "ACTIVE"})
    got = prov.find_user(email="pat@example.com")
    assert got.user_id == "u-9" and got.status == "ACTIVE" and len(sent) == 1
    assert sent[0].url.path == "/security/v1/service/users/lookup"
    assert sent[0].url.params["email"] == "pat@example.com"
    assert _provisioner(404)[0].find_user(email="x@example.com") is None
    for status in (401, 403, 500):
        p, s = _provisioner(status)
        with pytest.raises(ProvisioningError):
            p.find_user(email="x@example.com")
        assert len(s) == 1


def test_provisioner_marks_an_employee_once_with_the_service_token():
    prov, sent = _provisioner(200, {"userId": "u-9", "isEmployee": True})
    prov.mark_employee(user_id="u-9")
    assert len(sent) == 1 and sent[0].method == "POST"
    assert sent[0].url.path == "/security/v1/service/users/u-9/employee"
    assert sent[0].headers["authorization"] == "Bearer svc"
    for status in (403, 404, 500):
        p, s = _provisioner(status)
        with pytest.raises(ProvisioningError):
            p.mark_employee(user_id="u-9")
        assert len(s) == 1


def test_provisioner_sets_a_password_once_and_maps_the_answers():
    prov, sent = _provisioner(200, {"userId": "u-9", "primaryEmail": "a@b.co"})
    assert prov.set_password(user_id="u-9", password="Temp-Pass-123") == "a@b.co"
    assert len(sent) == 1 and sent[0].url.path == "/security/v1/service/users/u-9/password"
    assert b"Temp-Pass-123" in sent[0].content and sent[0].headers["authorization"] == "Bearer svc"
    for status, code in (
        (409, "LOGIN_NOT_ACTIVE"),
        (404, "LOGIN_NOT_FOUND"),
        (403, "NOT_PERMITTED"),
        (500, "SECURITY_UNAVAILABLE"),
    ):
        p, s = _provisioner(status)
        with pytest.raises(ProvisioningError) as err:
            p.set_password(user_id="u-9", password="Temp-Pass-123")
        assert err.value.code == code and len(s) == 1


def test_district_university_college_round_trip_and_pending_details_clear_themselves(make_client):
    client, _ = make_client()
    r = client.post("/hr/v1/employees", json=payload(), headers=auth(HR))
    assert r.status_code == 201
    emp = r.json()["employee"]
    eid = emp["employeeId"]
    assert emp["district"] is None
    assert {
        "DISTRICT",
        "STATE",
        "PINCODE",
        "EMERGENCY_CONTACT",
        "EXPERIENCE",
        "QUALIFICATION",
        "SALARY",
    } <= set(emp["missingDetails"])
    q = {"degree_code": "BCOM", "percentage": 70, "year_of_passing": 2020}
    added = client.post(f"/hr/v1/employees/{eid}/qualifications", json=q, headers=auth(HR))
    assert added.status_code == 201
    now = client.get(f"/hr/v1/employees/{eid}", headers=auth(HR)).json()
    assert (
        "UNIVERSITY_COLLEGE" in now["missingDetails"]
        and "QUALIFICATION" not in now["missingDetails"]
    )
    qid = now["qualifications"][0]["qualificationId"]
    done = client.put(
        f"/hr/v1/employees/{eid}/qualifications/{qid}",
        json={**q, "university": "Utkal University", "college": "Ravenshaw College"},
        headers=auth(HR),
    )
    assert done.status_code == 200
    client.patch(f"/hr/v1/employees/{eid}", json={"district": "Cuttack"}, headers=auth(HR))
    after = client.get(f"/hr/v1/employees/{eid}", headers=auth(HR)).json()
    assert after["district"] == "Cuttack"
    assert after["qualifications"][0]["university"] == "Utkal University"
    assert "DISTRICT" not in after["missingDetails"]
    assert "UNIVERSITY_COLLEGE" not in after["missingDetails"]
