"""Shared fakes for the attendance, leave, claims and payroll tests."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import text

from hrmgmt import permissions as perm
from hrmgmt.config import Settings
from hrmgmt.main import create_app
from hrmgmt.provisioning import CreatedLogin, FoundLogin, ProvisioningError, SyncOutcome
from hrmgmt.storage import StorageError
from tests.test_api import FakeAuthorizer, FakeValidator

IST = ZoneInfo("Asia/Kolkata")


def ist(year, month, day, hour=10, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=IST).astimezone(UTC)


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
    def __init__(self):
        self.existing: dict[str, FoundLogin] = {}

    def create_user(self, **kwargs):
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


class FakeGeocoder:
    def __init__(self, address: str | None = "Station Road, Cuttack, Odisha 753001, India"):
        self.address_value = address
        self.calls = 0

    def address(self, latitude, longitude):
        self.calls += 1
        return self.address_value


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, now: datetime) -> None:
        self.now = now


def settings() -> Settings:
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


class World:
    """An app wired to fakes, with helpers to create employees and project assignments."""

    def __init__(
        self, engine, grants: dict[str, set[str]] | None = None, now: datetime | None = None
    ):
        self.engine = engine
        self.clock = Clock(now or ist(2026, 10, 5, 10, 20))  # a Monday
        self.storage = FakeStorage()
        self.geocoder = FakeGeocoder()
        self.grants = grants if grants is not None else {}
        app = create_app(settings())
        app.state.validator = FakeValidator()
        self.authorizer = FakeAuthorizer(self.grants)
        self.authorizer.grants = self.grants  # an empty dict is falsy: keep our own reference
        app.state.authorizer = self.authorizer
        app.state.provisioner = FakeProvisioner()
        app.state.storage = self.storage
        app.state.geocoder = self.geocoder
        app.state.clock = self.clock
        self.app = app
        self.client = TestClient(app, raise_server_exceptions=False)

    def grant(self, user_id: str, *keys: str) -> str:
        self.grants.setdefault(user_id, set()).update(keys)
        return user_id

    def headers(self, user_id: str) -> dict[str, str]:
        return {"Authorization": f"Bearer user:{user_id}"}

    def employee(self, hr_user: str, **over) -> tuple[dict, str]:
        """Create an employee through the API as HR; returns (employee, security user id)."""
        n = uuid.uuid4().hex[:8]
        body = {
            "employee_code": f"W{n}".upper(),
            "full_name": "Test Person",
            "personal_email": f"t.{n}@example.com",
            "mobile": "9876543210",
        }
        body.update(over)
        self.grant(hr_user, perm.HR_EMPLOYEE_MANAGE, perm.HR_EMPLOYEE_READ)
        emp = self.client.post("/hr/v1/employees", json=body, headers=self.headers(hr_user)).json()[
            "employee"
        ]
        with self.engine.connect() as conn:
            user = str(
                conn.execute(
                    text(
                        "SELECT security_user_id FROM hr.employee WHERE employee_id = CAST(:i AS uuid)"
                    ),
                    {"i": emp["employeeId"]},
                ).scalar_one()
            )
        return emp, user

    def assign(
        self,
        user_id: str,
        role: str,
        *,
        tenant: str = "tenant-a",
        outlet: tuple[str, float, float] | None = None,
        valid_from: datetime | None = None,
        project: tuple[str, str] = ("P1", "Project One"),
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO hr.work_assignment (security_user_id, tenant_id, project_code,"
                    " project_name, role_code, outlet_id, outlet_name, latitude, longitude,"
                    " valid_from, last_seen_at) VALUES (CAST(:u AS uuid), :t, :pc, :pn,"
                    " :r, CAST(:o AS uuid), :on, :la, :lo, :vf, now())"
                ),
                {
                    "u": user_id,
                    "t": tenant,
                    "pc": project[0],
                    "pn": project[1],
                    "r": role,
                    "o": str(uuid.uuid4()) if outlet else None,
                    "on": outlet[0] if outlet else None,
                    "la": outlet[1] if outlet else None,
                    "lo": outlet[2] if outlet else None,
                    "vf": valid_from or datetime(2026, 1, 1, tzinfo=UTC),
                },
            )

    def clean_assignments(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM hr.work_assignment"))


def jpeg(size=(320, 240), color=(120, 160, 200)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="JPEG")
    return out.getvalue()


OUTLET = ("Cuttack Motors", 20.4625, 85.8828)
NEAR = (20.4630, 85.8830)  # about 60 m away
FAR = (20.5500, 85.9500)  # roughly 12 km away
__all__ = ["FAR", "NEAR", "OUTLET", "World", "ist", "jpeg", "timedelta"]
