"""Global safety switch, backlog, and environment status."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.config import settings
from app.core.time import iso, utcnow
from app.db import get_session, ping
from app.models import AllowedTestRecipient, Suppression
from app.providers.registry import provider_catalog
from app.schemas import PauseRequest, RetentionUpdateRequest
from app.security import Principal, client_key, require_admin
from app.services import settings as app_settings
from app.services.queue import backlog_snapshot
from app.services.audit import record_audit
from app.services.secrets import get_secret_store

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/status")
async def status(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    try:
        await ping()
        database = "ok"
    except Exception:  # noqa: BLE001
        database = "unavailable"

    try:
        get_secret_store().probe()
        secret_store = "ok"
    except Exception as exc:  # noqa: BLE001
        secret_store = f"error: {type(exc).__name__}"

    active_suppressions = await session.scalar(
        select(func.count()).select_from(Suppression).where(Suppression.removed_at.is_(None))
    )
    recipients = await session.scalar(
        select(func.count()).select_from(AllowedTestRecipient)
    )

    return {
        "version": __version__,
        "app_env": settings.app_env,
        "now": iso(utcnow()),
        "database": database,
        "secret_store": secret_store,
        "secret_backend": settings.secret_backend,
        "global_sending_enabled": await app_settings.is_global_sending_enabled(session),
        "backlog": await backlog_snapshot(session),
        "providers": provider_catalog(),
        "llm_enabled": settings.llm_enabled,
        "retention": await app_settings.retention_settings(session),
        "counts": {
            "active_suppressions": int(active_suppressions or 0),
            "test_recipients": int(recipients or 0),
        },
        "limits": {
            "send_rate_limit_per_minute": settings.send_rate_limit_per_minute,
            "max_message_bytes": settings.max_message_bytes,
        },
    }


@router.post("/pause")
async def pause_sending(
    payload: PauseRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    await app_settings.set_global_sending(
        session,
        enabled=False,
        actor=principal.actor,
        reason=payload.reason,
        request_id=principal.request_id,
        source_ip=client_key(request),
    )
    await session.commit()
    return {
        "global_sending_enabled": False,
        "message": "Future submissions are blocked. Already accepted messages cannot be recalled.",
    }


@router.post("/resume")
async def resume_sending(
    payload: PauseRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    await app_settings.set_global_sending(
        session,
        enabled=True,
        actor=principal.actor,
        reason=payload.reason,
        request_id=principal.request_id,
        source_ip=client_key(request),
    )
    await session.commit()
    return {
        "global_sending_enabled": True,
        "message": "Future submissions are allowed again within each account's policy.",
    }


@router.post("/retention")
async def update_retention(
    payload: RetentionUpdateRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    values = {
        "body_days": payload.body_days,
        "event_days": payload.event_days,
        "audit_days": payload.audit_days,
    }
    await app_settings.set_setting(
        session, app_settings.RETENTION_KEY, values, updated_by=principal.actor
    )
    await record_audit(
        session,
        actor=principal.actor,
        action="system.retention_update",
        target_type="app_setting",
        request_id=principal.request_id,
        source_ip=client_key(request),
        details=values,
    )
    await session.commit()
    return {"retention": values, "note": "Backups age out on their own schedule."}
