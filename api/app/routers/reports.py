"""Reporting, audit feed, and suppression management."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.db import get_session
from app.models import Suppression
from app.schemas import CancelRequest
from app.security import Principal, client_key, require_admin
from app.serializers import suppression_payload
from app.services import reports as report_service
from app.services.suppression import remove_suppression

router = APIRouter(tags=["reports"])


@router.get("/reports/summary")
async def summary(
    days: int = Query(default=7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    return await report_service.summary(session, days=days)


@router.get("/reports/audit")
async def audit(
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    return {"entries": await report_service.audit_feed(session, limit=limit)}


@router.get("/reports/safety")
async def safety(
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    return {"events": await report_service.safety_feed(session, limit=limit)}


@router.get("/suppressions")
async def list_suppressions(
    active_only: bool = Query(default=True),
    limit: int = Query(default=200, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    statement = select(Suppression).order_by(Suppression.created_at.desc()).limit(limit)
    if active_only:
        statement = statement.where(Suppression.removed_at.is_(None))
    rows = list(await session.scalars(statement))
    return {"suppressions": [suppression_payload(row) for row in rows]}


@router.delete("/suppressions/{suppression_id}")
async def delete_suppression(
    suppression_id: uuid.UUID,
    payload: CancelRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    entry = await session.get(Suppression, suppression_id)
    if entry is None:
        raise NotFound("Suppression not found")
    await remove_suppression(
        session,
        suppression_id=suppression_id,
        actor=principal.actor,
        reason=payload.reason,
        request_id=principal.request_id,
        source_ip=client_key(request),
    )
    await session.commit()
    return {
        "removed": True,
        "id": str(suppression_id),
        "note": "Removal is audited. Sending to this recipient is allowed again immediately.",
    }
