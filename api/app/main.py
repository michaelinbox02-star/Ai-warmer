"""FastAPI application entrypoint.

Run locally with::

    uvicorn app.main:app --host 127.0.0.1 --port 8000

The API is private: every data endpoint requires either an operator session
cookie or the configured automation bearer token. There is no public signup or
public webhook. Provider events enter through an explicitly enabled local relay
endpoint.
"""

from __future__ import annotations

import logging
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app import __version__
from app.config import settings
from app.context import request_id_var
from app.core.errors import AppError
from app.db import dispose_engine, ping
from app.logging import configure_logging
from app.routers import accounts, auth, events, messages, reports, system, tests
from app.services.secrets import get_secret_store

logger = logging.getLogger("app.api")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}
_API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request id, no-store headers, and basic browser hardening."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        incoming = request.headers.get("x-request-id")
        request_id = incoming if incoming and _REQUEST_ID_RE.fullmatch(incoming) else uuid.uuid4().hex
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)

        response.headers["X-Request-ID"] = request_id
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)

        path = request.url.path
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
            response.headers.setdefault("Pragma", "no-cache")
            response.headers.setdefault("Content-Security-Policy", _API_CSP)
        elif path in {"/healthz", "/readyz"}:
            response.headers.setdefault("Cache-Control", "no-store")

        if settings.is_production and settings.public_base_url.startswith("https://"):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    logger.info(
        "Starting deliverability API version=%s env=%s",
        __version__,
        settings.app_env,
    )

    try:
        await ping()
        logger.info("Database connection verified")
    except Exception as exc:  # noqa: BLE001 - app remains up so /readyz can report
        logger.warning("Database is not reachable at startup: %s", type(exc).__name__)

    try:
        get_secret_store().probe()
        logger.info("Secret store available backend=%s", settings.secret_backend)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Secret store probe failed: %s", type(exc).__name__)

    if not (settings.admin_password or settings.admin_password_hash):
        logger.warning("No operator password configured; sign-in is impossible")
    if not settings.api_token:
        logger.info("Automation API token is disabled")
    if not settings.event_ingest_token:
        logger.info("Provider event relay intake is disabled")

    try:
        yield
    finally:
        await dispose_engine()
        logger.info("Deliverability API stopped")


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def create_app() -> FastAPI:
    app = FastAPI(
        title="Private Deliverability API",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Authorization",
            "X-CSRF-Token",
            "Idempotency-Key",
            "X-Request-ID",
            "X-Event-Token",
        ],
        expose_headers=["X-Request-ID", "Idempotent-Replay"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        payload = exc.payload()
        request_id = _request_id(request)
        if request_id:
            payload["request_id"] = request_id
        headers: dict[str, str] = {}
        if exc.status_code == 429:
            retry_after = exc.details.get("retry_after_seconds")
            if isinstance(retry_after, int) and retry_after > 0:
                headers["Retry-After"] = str(retry_after)
        if exc.status_code >= 500:
            logger.error(
                "Application error code=%s request_id=%s", exc.code, request_id
            )
        return JSONResponse(
            status_code=exc.status_code, content=payload, headers=headers or None
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Never echo ``input``: request models may contain passwords or tokens.
        issues = [
            {
                "location": [str(part) for part in error.get("loc", ())],
                "message": str(error.get("msg", "Invalid value")),
                "type": str(error.get("type", "value_error")),
            }
            for error in exc.errors()
        ]
        body: dict[str, Any] = {
            "error": {
                "code": "validation_error",
                "message": "Request validation failed",
                "details": {"issues": issues},
            }
        }
        request_id = _request_id(request)
        if request_id:
            body["request_id"] = request_id
        return JSONResponse(status_code=422, content=body)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        codes = {
            401: "authentication_required",
            403: "permission_denied",
            404: "not_found",
            405: "method_not_allowed",
            413: "payload_too_large",
            429: "rate_limited",
        }
        body: dict[str, Any] = {
            "error": {
                "code": codes.get(exc.status_code, f"http_{exc.status_code}"),
                "message": exc.detail if isinstance(exc.detail, str) else "Request failed",
            }
        }
        request_id = _request_id(request)
        if request_id:
            body["request_id"] = request_id
        return JSONResponse(
            status_code=exc.status_code, content=body, headers=exc.headers
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = _request_id(request)
        logger.exception(
            "Unhandled exception path=%s request_id=%s",
            request.url.path,
            request_id,
        )
        body: dict[str, Any] = {
            "error": {
                "code": "internal_error",
                "message": "An internal error occurred. Check the server log with the request id.",
            }
        }
        if request_id:
            body["request_id"] = request_id
        return JSONResponse(status_code=500, content=body)

    @app.get("/healthz", tags=["health"], include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["health"], include_in_schema=False)
    async def readyz() -> JSONResponse:
        database = "ok"
        secrets = "ok"
        try:
            await ping()
        except Exception:  # noqa: BLE001
            database = "unavailable"
        try:
            get_secret_store().probe()
        except Exception:  # noqa: BLE001
            secrets = "unavailable"
        ready = database == "ok" and secrets == "ok"
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "not_ready",
                "version": __version__,
                "database": database,
                "secret_store": secrets,
            },
        )

    for api_router in (
        auth.router,
        accounts.router,
        messages.router,
        tests.router,
        reports.router,
        events.router,
        system.router,
    ):
        app.include_router(api_router, prefix="/api/v1")

    return app


app = create_app()
