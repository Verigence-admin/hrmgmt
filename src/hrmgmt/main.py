from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from hrmgmt.api.admin import router as admin_router
from hrmgmt.api.attendance import router as attendance_router
from hrmgmt.api.attendance_reports import router as attendance_reports_router
from hrmgmt.api.claims import router as claims_router
from hrmgmt.api.designation_salary_import import router as designation_salary_import_router
from hrmgmt.api.employee_import import router as employee_import_router
from hrmgmt.api.employee_sync import router as employee_sync_router
from hrmgmt.api.employees import router as employees_router
from hrmgmt.api.housekeeping import router as housekeeping_router
from hrmgmt.api.leave import router as leave_router
from hrmgmt.api.messages import router as messages_router
from hrmgmt.api.meta import router as meta_router
from hrmgmt.api.payroll import router as payroll_router
from hrmgmt.api.tickets import router as tickets_router
from hrmgmt.authz import SecurityAuthorizer
from hrmgmt.config import Settings, get_settings
from hrmgmt.db import get_engine
from hrmgmt.errors import install_error_handlers
from hrmgmt.geocode import GoogleReverseGeocoder
from hrmgmt.mailer import SmtpMailer
from hrmgmt.provisioning import SecurityUserProvisioner
from hrmgmt.security import SecurityTokenValidator
from hrmgmt.storage import S3Storage
from hrmgmt.workcontext import DailySync, WorkContextClient

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
    app.state.storage = None
    app.state.geocoder = None
    app.state.workcontext = None
    app.state.clock = None
    app.state.mailer = None
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

    if settings.storage_configured:
        app.state.storage = S3Storage(
            endpoint_url=settings.storage_endpoint,
            bucket=settings.storage_bucket,
            access_key_id=settings.storage_access_key_id,
            secret_access_key=settings.storage_secret_access_key,
            region=settings.storage_region,
        )
    else:
        logger.warning("hr_file_storage_not_configured")

    app.include_router(meta_router)
    if settings.google_maps_api_key:
        app.state.geocoder = GoogleReverseGeocoder(settings.google_maps_api_key)
    else:
        logger.warning("hr_reverse_geocoding_not_configured")
    if settings.audit_core_base_url and app.state.authorizer is not None:
        app.state.workcontext = WorkContextClient(
            base_url=settings.audit_core_base_url, token_provider=app.state.authorizer.service_token
        )
    else:
        logger.warning("hr_work_context_not_configured")

    if settings.mail_configured:
        app.state.mailer = SmtpMailer(
            host=settings.smtp_host,
            port=settings.smtp_port,
            user=settings.smtp_user,
            password=settings.smtp_password,
            from_address=settings.smtp_from or None,
        )
    else:
        logger.warning("hr_email_not_configured")

    app.include_router(admin_router)
    app.include_router(attendance_router)
    app.include_router(attendance_reports_router)
    app.include_router(leave_router)
    app.include_router(claims_router)
    app.include_router(payroll_router)
    app.include_router(employee_import_router)
    app.include_router(designation_salary_import_router)
    app.include_router(employee_sync_router)
    app.include_router(messages_router)
    app.include_router(tickets_router)
    app.include_router(housekeeping_router)
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
    app = create_app()
    if app.state.workcontext is not None:
        # Once a day, in the background: HR requests never wait on Audit Core.
        daily = DailySync(get_engine(), app.state.workcontext)

        @asynccontextmanager
        async def lifespan(_: FastAPI) -> AsyncIterator[None]:
            daily.start()
            try:
                yield
            finally:
                daily.stop()

        app.router.lifespan_context = lifespan
    return app
