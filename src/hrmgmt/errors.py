from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)


class ApiError(Exception):
    """A deliberate, client-safe error with a stable machine code."""

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


def unauthenticated(detail: str = "Sign in again.") -> ApiError:
    return ApiError(401, "HR_AUTHENTICATION_FAILED", detail)


def forbidden(detail: str = "You do not have access to this.") -> ApiError:
    return ApiError(403, "HR_PERMISSION_DENIED", detail)


def not_found(detail: str = "Not found.") -> ApiError:
    return ApiError(404, "HR_NOT_FOUND", detail)


def conflict(code: str, detail: str) -> ApiError:
    return ApiError(409, code, detail)


def dependency_unavailable(detail: str) -> ApiError:
    return ApiError(503, "HR_DEPENDENCY_UNAVAILABLE", detail)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content={"code": exc.code, "detail": exc.detail}
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Field names and messages only: never echo the submitted values back (PAN, bank, etc.).
        problems = [
            {
                "field": ".".join(str(p) for p in e.get("loc", ()) if p != "body"),
                "message": e.get("msg", ""),
            }
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "code": "HR_VALIDATION_FAILED",
                "detail": "Some fields are not valid.",
                "problems": problems,
            },
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error("hr_unhandled_error", path=request.url.path, error_type=type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "code": "HR_INTERNAL_ERROR",
                "detail": "Something went wrong. Please try again.",
            },
        )
