"""Recipient suppression (hard bounces, complaints, operator blocks)."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utcnow
from app.models import Suppression
from app.services.audit import record_audit, record_safety_event


async def active_suppression(session: AsyncSession, email: str) -> Suppression | None:
    normalized = email.strip().lower()
    return await session.scalar(
        select(Suppression)
        .where(func.lower(Suppression.recipient_email) == normalized, Suppression.removed_at.is_(None))
        .limit(1)
    )


async def add_suppression(
    session: AsyncSession,
    *,
    recipient_email: str,
    reason: str,
    actor: str = "system",
    source_account_id: uuid.UUID | None = None,
    source_message_id: uuid.UUID | None = None,
    evidence_event_id: uuid.UUID | None = None,
    detail: str | None = None,
) -> Suppression | None:
    existing = await active_suppression(session, recipient_email)
    if existing is not None:
        return None

    entry = Suppression(
        recipient_email=recipient_email.strip(),
        reason=reason,
        source_account_id=source_account_id,
        source_message_id=source_message_id,
        evidence_event_id=evidence_event_id,
    )
    session.add(entry)
    await session.flush()

    await record_safety_event(
        session,
        severity="critical" if reason in {"complaint", "hard_bounce"} else "warning",
        event_type="recipient_suppressed",
        action_taken="future sends blocked for this recipient",
        account_id=source_account_id,
        message_id=source_message_id,
        details={"reason": reason, "detail": detail},
    )
    return entry


async def remove_suppression(
    session: AsyncSession,
    *,
    suppression_id: uuid.UUID,
    actor: str,
    reason: str,
    request_id: str | None = None,
    source_ip: str | None = None,
) -> Suppression:
    entry = await session.get(Suppression, suppression_id)
    if entry is None:
        from app.core.errors import NotFound

        raise NotFound("Suppression not found")
    if entry.removed_at is not None:
        return entry
    entry.removed_at = utcnow()
    entry.removed_by = actor
    entry.removal_reason = reason
    await record_audit(
        session,
        actor=actor,
        action="suppression.remove",
        target_type="suppression",
        target_id=entry.id,
        request_id=request_id,
        source_ip=source_ip,
        details={"recipient": entry.recipient_email, "reason": reason},
    )
    return entry
