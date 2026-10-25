"""Provider event intake.

Events are the only way a message becomes `delivered`, `bounced`, or
`complained`. Provider acceptance alone never sets `delivered`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.core.time import ensure_utc, utcnow
from app.models import EmailAccount, EmailMessage, ProviderEvent, SendPolicy
from app.services import policy as policy_service
from app.services.audit import record_safety_event
from app.services.suppression import add_suppression

TERMINAL_EVIDENCE_STATUSES = frozenset({"delivered", "bounced", "complained"})


def _is_hard_bounce(event_type: str, bounce_type: str | None, diagnostic_code: str | None) -> bool:
    if bounce_type:
        return bounce_type.lower() == "hard"
    if diagnostic_code:
        return diagnostic_code.strip().startswith("5")
    return event_type == "bounce"


async def _resolve_message(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    message_id: uuid.UUID | None,
    provider_message_id: str | None,
    recipient_email: str | None,
) -> tuple[EmailMessage | None, str]:
    """Resolve an event to a message and report how confident the match is.

    Only ``message_id`` and ``provider_message_id`` are strong enough to mutate
    message state. A recipient-only match is deliberately reported as weak: it
    is useful context, but it could belong to a different message to the same
    person.
    """

    if message_id is not None:
        message = await session.get(EmailMessage, message_id)
        if message is not None and message.account_id == account_id:
            return message, "message_id"

    if provider_message_id:
        message = await session.scalar(
            select(EmailMessage)
            .where(
                EmailMessage.account_id == account_id,
                EmailMessage.provider_message_id == provider_message_id,
            )
            .order_by(EmailMessage.submitted_at.desc().nullslast())
            .limit(1)
        )
        if message is not None:
            return message, "provider_message_id"

    if recipient_email:
        cutoff = utcnow() - timedelta(days=30)
        message = await session.scalar(
            select(EmailMessage)
            .where(
                EmailMessage.account_id == account_id,
                EmailMessage.to_email.ilike(recipient_email.strip()),
                EmailMessage.created_at >= cutoff,
            )
            .order_by(EmailMessage.created_at.desc())
            .limit(1)
        )
        if message is not None:
            return message, "recipient"

    return None, "none"


async def ingest_event(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    event: dict[str, Any],
) -> tuple[ProviderEvent, bool]:
    account = await session.get(EmailAccount, account_id)
    if account is None:
        raise NotFound("Account not found")

    provider_event_id = event.get("provider_event_id")
    if provider_event_id:
        existing = await session.scalar(
            select(ProviderEvent).where(
                ProviderEvent.account_id == account_id,
                ProviderEvent.provider_event_id == provider_event_id,
            )
        )
        if existing is not None:
            return existing, True

    event_type = str(event["event_type"])
    event_at = ensure_utc(event.get("event_at")) or utcnow()
    recipient_email = event.get("recipient_email")
    diagnostic_code = event.get("diagnostic_code")
    bounce_type = event.get("bounce_type")

    message = await _resolve_message(
        session,
        account_id=account_id,
        message_id=event.get("message_id"),
        provider_message_id=event.get("provider_message_id"),
        recipient_email=recipient_email,
    )

    row = ProviderEvent(
        account_id=account_id,
        message_id=message.id if message else None,
        provider=str(event.get("provider") or account.provider),
        provider_event_id=provider_event_id,
        event_type=event_type,
        event_at=event_at,
        recipient_email=recipient_email,
        diagnostic_code=diagnostic_code,
        sanitized_detail=event.get("detail"),
        event_metadata=event.get("metadata") or {},
    )
    session.add(row)
    await session.flush()

    if message is None:
        await record_safety_event(
            session,
            severity="warning",
            event_type="provider_event_unmatched",
            action_taken="event recorded without a matching message",
            account_id=account_id,
            details={"event_type": event_type, "provider_event_id": provider_event_id},
        )
        return row, False

    policy = await session.get(SendPolicy, account_id)
    strong = match in {"message_id", "provider_message_id"}

    if not strong:
        await record_safety_event(
            session,
            severity="warning",
            event_type="provider_event_weak_match",
            action_taken="event linked by recipient only; message state not changed",
            account_id=account_id,
            message_id=message.id,
            details={
                "event_type": event_type,
                "provider_event_id": provider_event_id,
                "match": match,
            },
        )
    else:
        message.updated_at = utcnow()

    if event_type == "delivery":
        if strong:
            message.delivery_evidence_at = event_at
            # A delivery event that arrives while the worker is finalizing is
            # promoted by finalize_submission. Otherwise evidence wins and
            # stops any pending retry for this message.
            if message.status != "sending":
                message.status = "delivered"
                message.next_attempt_at = None
        # Weak matches are recorded for the operator but cannot promote state.

    elif event_type == "bounce":
        hard = _is_hard_bounce(event_type, bounce_type, diagnostic_code)
        if hard:
            if strong:
                if message.status != "delivered":
                    message.status = "bounced"
                    message.next_attempt_at = None
                message.final_error_code = diagnostic_code or "hard_bounce"
                message.final_error_summary = event.get("detail") or "Recipient bounced (hard)"
            await add_suppression(
                session,
                recipient_email=(recipient_email or message.to_email),
                reason="hard_bounce",
                source_account_id=account_id,
                source_message_id=message.id if strong else None,
                evidence_event_id=row.id,
                detail=diagnostic_code,
            )
        else:
            await record_safety_event(
                session,
                severity="warning",
                event_type="soft_bounce",
                action_taken="no suppression; delivery still unconfirmed",
                account_id=account_id,
                message_id=message.id,
                details={"diagnostic_code": diagnostic_code, "match": match},
            )

    elif event_type == "complaint":
        if strong:
            message.status = "complained"
            message.next_attempt_at = None
            message.final_error_code = diagnostic_code or "complaint"
            message.final_error_summary = event.get("detail") or "Recipient marked the message as spam"
        await add_suppression(
            session,
            recipient_email=(recipient_email or message.to_email),
            reason="complaint",
            source_account_id=account_id,
            source_message_id=message.id if strong else None,
            evidence_event_id=row.id,
            detail=diagnostic_code,
        )
        if policy is not None:
            await policy_service.open_circuit(
                session,
                policy=policy,
                account=account,
                reason="complaint received for this account",
                trigger="complaint",
                requires_manual=True,
                message_id=message.id if strong else None,
            )

    elif event_type == "rejected":
        if strong and message.status in ("submitted",):
            message.status = "failed"
            message.final_error_code = diagnostic_code or "provider_rejected"
            message.final_error_summary = event.get("detail") or "Provider rejected the message"

    elif event_type == "quota":
        if policy is not None:
            await policy_service.open_circuit(
                session,
                policy=policy,
                account=account,
                reason=event.get("detail") or "provider quota event",
                trigger="quota_restriction",
                requires_manual=True,
                message_id=message.id if strong else None,
            )

    elif event_type == "authentication":
        await record_safety_event(
            session,
            severity="warning",
            event_type="provider_authentication_event",
            action_taken="diagnostic recorded; review domain authentication",
            account_id=account_id,
            message_id=message.id,
            details={"diagnostic_code": diagnostic_code, "detail": event.get("detail")},
        )

    if message.kind == "diagnostic_test" and message.status in {
        "delivered",
        "bounced",
        "complained",
        "failed",
    }:
        from app.services.test_runs import on_message_state_change

        await on_message_state_change(session, message)

    return row, False


async def recent_events(
    session: AsyncSession, *, message_id: uuid.UUID, limit: int = 50
) -> list[ProviderEvent]:
    rows = await session.scalars(
        select(ProviderEvent)
        .where(ProviderEvent.message_id == message_id)
        .order_by(ProviderEvent.event_at.desc())
        .limit(limit)
    )
    return list(rows)


def event_evidence_status(message: EmailMessage) -> str | None:
    if message.delivery_evidence_at is None:
        return None
    return "delivered"
