"""Audit trail and safety events.

Every state change with security or deliverability consequences is recorded
here. Callers are responsible for committing the surrounding transaction.
"""

from __future__ import annotations

import ipaddress
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, SafetyEvent
from app.core.time import utcnow


def _ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


async def record_audit(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    result: str = "success",
    request_id: str | None = None,
    source_ip: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor=actor[:200],
        action=action[:200],
        target_type=target_type,
        target_id=target_id,
        result=result,
        request_id=request_id,
        source_ip=_ip(source_ip),
        details=details or {},
    )
    session.add(entry)
    await session.flush()
    return entry


async def record_safety_event(
    session: AsyncSession,
    *,
    severity: str,
    event_type: str,
    action_taken: str,
    account_id: uuid.UUID | None = None,
    message_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> SafetyEvent:
    event = SafetyEvent(
        account_id=account_id,
        message_id=message_id,
        severity=severity,
        event_type=event_type,
        action_taken=action_taken,
        details=details or {},
        occurred_at=utcnow(),
    )
    session.add(event)
    await session.flush()
    return event
