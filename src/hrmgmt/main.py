from __future__ import annotations

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from hrmgmt.api.employees import router as employees_router
from hrmgmt.api.meta import router as meta_router
from hrmgmt.authz import SecurityAuthorizer
from hrmgmt.config import Settings, get_settings
from hrmgmt.db import get_engine
from hrmgmt.errors import install_error_handlers
from hrmgmt.provisioning import SecurityUserProvisioner
from hrmgmt.security import SecurityTokenValidator

logger = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Verigence HRMgmt", docs_url=None, redoc_url=None, openapi_url=None)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Correlation-ID"],
    )
    install_error_handlers(app)

    # A missing setting never crashes the service: /health stays up and every protected
    # request answers 503 with a clear message until the variables are supplied.
    app.state.validator = None
    app.state.authorizer = None
    app.state.provisioner = None
    if settings.security_jwks_url and settings.security_issuer and settings.security_audience:
        app.state.validator = SecurityTokenValidator(
            jwks_url=settings.security_jwks_url,
            issuer=settings.security_issuer,
            audience=settings.security_audience,
        )
    else:
        logger.warning("hr_sign_in_verification_not_configured")
    if settings.authz_configured:
        authorizer = SecurityAuthorizer(
            base_url=settings.security_base_url,
            client_id=settings.security_client_id,
            client_secret=settings.security_client_secret,
        )
        app.state.authorizer = authorizer
        app.state.provisioner = SecurityUserProvisioner(
            base_url=settings.security_base_url, token_provider=authorizer.service_token
        )
    else:
        logger.warning("hr_permission_checks_not_configured")

    app.include_router(meta_router)
    app.include_router(employees_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "hrmgmt"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1 FROM hr.audit_log LIMIT 1"))
        except Exception as exc:  # readiness must report, never raise
            logger.warning("hr_not_ready", error_type=type(exc).__name__)
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return JSONResponse(content={"status": "ready"})

    return app


def app_factory() -> FastAPI:
    return create_app()
