from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

# The native Verigence app runs in Capacitor WebViews with these stable origins.
_CAPACITOR_ORIGINS = ("capacitor://localhost", "https://localhost")


class ConfigurationError(RuntimeError):
    """A required environment variable is missing or malformed."""


def normalise_database_url(raw: str) -> str:
    """Return a SQLAlchemy psycopg3 URL for any common Postgres URL spelling."""
    value = raw.strip()
    for prefix in (
        "postgresql+psycopg://",
        "postgresql+asyncpg://",
        "postgresql://",
        "postgres://",
    ):
        if value.startswith(prefix):
            return "postgresql+psycopg://" + value[len(prefix) :]
    raise ConfigurationError("DATABASE_URL is not a PostgreSQL URL")


@dataclass(frozen=True)
class Settings:
    environment: str
    database_url: str
    security_base_url: str
    security_jwks_url: str
    security_issuer: str
    security_audience: str
    security_client_id: str
    security_client_secret: str
    storage_endpoint: str
    storage_bucket: str
    storage_access_key_id: str
    storage_secret_access_key: str
    storage_region: str
    allowed_origins: tuple[str, ...]
    db_pool_size: int
    db_max_overflow: int

    @property
    def storage_configured(self) -> bool:
        return bool(
            self.storage_endpoint
            and self.storage_bucket
            and self.storage_access_key_id
            and self.storage_secret_access_key
        )

    @property
    def authz_configured(self) -> bool:
        return bool(
            self.security_base_url and self.security_client_id and self.security_client_secret
        )


def _int(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not low <= value <= high:
        raise ConfigurationError(f"{name} must be between {low} and {high}")
    return value


def load_settings() -> Settings:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigurationError("DATABASE_URL is required")
    origins = [o.strip() for o in os.environ.get("HR_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    return Settings(
        environment=os.environ.get("HR_ENVIRONMENT", "DEV").strip() or "DEV",
        database_url=normalise_database_url(database_url),
        security_base_url=os.environ.get("SECURITY_BASE_URL", "").strip(),
        security_jwks_url=os.environ.get("SECURITY_JWKS_URL", "").strip(),
        security_issuer=os.environ.get("SECURITY_ISSUER", "").strip(),
        security_audience=os.environ.get("SECURITY_AUDIENCE", "").strip(),
        security_client_id=os.environ.get("SECURITY_CLIENT_ID", "").strip(),
        security_client_secret=os.environ.get("SECURITY_CLIENT_SECRET", ""),
        storage_endpoint=os.environ.get("HR_STORAGE_ENDPOINT", "").strip(),
        storage_bucket=os.environ.get("HR_STORAGE_BUCKET", "").strip(),
        storage_access_key_id=os.environ.get("HR_STORAGE_ACCESS_KEY_ID", "").strip(),
        storage_secret_access_key=os.environ.get("HR_STORAGE_SECRET_ACCESS_KEY", ""),
        storage_region=os.environ.get("HR_STORAGE_REGION", "auto").strip() or "auto",
        allowed_origins=tuple(dict.fromkeys([*origins, *_CAPACITOR_ORIGINS])),
        db_pool_size=_int("HR_DB_POOL_SIZE", 5, 1, 20),
        db_max_overflow=_int("HR_DB_MAX_OVERFLOW", 5, 0, 20),
    )


@lru_cache
def get_settings() -> Settings:
    return load_settings()
