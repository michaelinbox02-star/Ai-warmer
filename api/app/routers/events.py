"""Provider event relay intake.

This is intentionally not a public webhook endpoint. A local relay (cron,
provider polling script, or your own webhook receiver) posts here with a shared
token. `provider_event_id` makes ingestion idempotent.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import PermissionDenied
from app.db import get_session
from app.schemas import SystemEventRequest
from app.serializers import provider_event_payload
from app.services.audit import record_audit
from app.services.events import ingest_event

router = APIRouter(prefix="/events", tags=["events"])


async def _require_event_token(x_event_token: str | None = Header(default=None, alias="X-Event-Token")) -> None:
    if not settings.event_ingest_token:
        raise PermissionDenied(
            "Provider event intake is disabled (EVENT_INGEST_TOKEN is not configured)",
            code="event_intake_disabled",
        )
    if not x_event_token or not hmac.compare_digest(x_event_token, settings.event_ingest_token):
        raise PermissionDenied("Invalid event ingest token", code="invalid_event_token")


@router.post("/provider", dependencies=[Depends(_require_event_token)])
async def provider_event(
    payload: SystemEventRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row, duplicate = await ingest_event(
        session,
        account_id=payload.account_id,
        event=payload.model_dump(),
    )
    await record_audit(
        session,
        actor="event-relay",
        action="provider_event.ingest",
        target_type="provider_event",
        target_id=row.id,
        source_ip=request.client.host if request.client else None,
        details={
            "event_type": row.event_type,
            "duplicate": duplicate,
            "account_id": str(payload.account_id),
        },
    )
    await session.commit()
    return {
        "event": provider_event_payload(row),
        "duplicate": duplicate,
        "delivery_note": "Delivery status changes only with provider evidence, never from acceptance alone.",
    }
