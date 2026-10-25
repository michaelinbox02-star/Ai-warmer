"""Operator session endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import AuthenticationError, PermissionDenied
from app.db import get_session
from app.schemas import LoginRequest, SessionInfo
from app.security import (
    CSRF_COOKIE,
    Principal,
    clear_session_cookies,
    client_key,
    issue_session_token,
    login_throttle,
    new_csrf_token,
    optional_admin,
    require_admin,
    set_session_cookies,
    verify_operator_password,
)
from app.services.audit import record_audit

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=SessionInfo)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> SessionInfo:
    key = client_key(request)
    login_throttle.assert_allowed(key)

    origin = request.headers.get("origin")
    if origin and origin not in settings.allowed_origins:
        raise PermissionDenied("Origin is not allowed", code="origin_not_allowed")

    if not verify_operator_password(payload.password):
        login_throttle.record_failure(key)
        await record_audit(
            session,
            actor="anonymous",
            action="auth.login",
            result="denied",
            source_ip=key,
            details={"reason": "invalid_credentials"},
        )
        await session.commit()
        raise AuthenticationError("Invalid credentials")

    login_throttle.record_success(key)
    token = issue_session_token()
    csrf = new_csrf_token()
    set_session_cookies(response, token=token, csrf=csrf)

    await record_audit(
        session,
        actor="operator",
        action="auth.login",
        result="success",
        source_ip=key,
        details={"method": "session"},
    )
    await session.commit()

    return SessionInfo(
        authenticated=True,
        actor="operator",
        method="session",
        csrf_token=csrf,
        session_expires_in=settings.session_ttl_minutes * 60,
    )


@router.post("/logout", response_model=SessionInfo)
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> SessionInfo:
    clear_session_cookies(response)
    await record_audit(
        session,
        actor=principal.actor,
        action="auth.logout",
        request_id=principal.request_id,
        source_ip=client_key(request),
    )
    await session.commit()
    return SessionInfo(authenticated=False)


@router.get("/session", response_model=SessionInfo)
async def session_info(
    request: Request,
    principal: Principal | None = Depends(optional_admin),
) -> SessionInfo:
    if principal is None:
        return SessionInfo(authenticated=False)
    return SessionInfo(
        authenticated=True,
        actor=principal.actor,
        method=principal.method,
        csrf_token=request.cookies.get(CSRF_COOKIE),
        session_expires_in=settings.session_ttl_minutes * 60,
    )
