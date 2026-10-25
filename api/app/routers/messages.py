"""Transactional message queueing, inspection, and operator overrides."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import Conflict, NotFound, PolicyViolation, ValidationError
from app.core.idempotency import (
    fingerprint_matches,
    request_fingerprint,
    validate_idempotency_key,
)
from app.core.time import iso, utcnow
from app.db import get_session
from app.models import (
    DeliveryAttempt,
    DiagnosticReport,
    EmailAccount,
    EmailMessage,
    SendPolicy,
)
from app.schemas import CancelRequest, DiagnosisRequest, MessageCreateRequest, RequeueRequest
from app.security import Principal, client_key, enforce_send_rate_limit, require_admin
from app.serializers import diagnostic_payload, message_detail, message_summary
from app.services import policy as policy_service
from app.services.audit import record_audit, record_safety_event
from app.services.diagnostics import diagnose_message
from app.services.events import recent_events
from app.services.suppression import active_suppression

router = APIRouter(prefix="/messages", tags=["messages"])


def _request_context(request: Request, principal: Principal) -> dict[str, Any]:
    return {
        "request_id": principal.request_id,
        "source_ip": request.client.host if request.client else None,
    }


@router.post("", status_code=201)
async def create_message(
    payload: MessageCreateRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
    _limit: None = Depends(enforce_send_rate_limit),
) -> dict[str, Any]:
    if not idempotency_key:
        raise ValidationError(
            "The Idempotency-Key header is required for message submission",
            code="idempotency_key_required",
        )
    try:
        key = validate_idempotency_key(idempotency_key)
    except ValueError as exc:
        raise ValidationError(str(exc), code="invalid_idempotency_key") from exc

    account = await session.get(EmailAccount, payload.account_id)
    if account is None:
        raise NotFound("Account not found")
    if account.state == "disabled":
        raise PolicyViolation("Account is disabled", code="account_disabled")

    policy = await session.get(SendPolicy, account.id)
    if policy is None:
        raise PolicyViolation("Send policy is missing", code="policy_missing")
    if policy.mode == "paused":
        raise PolicyViolation("Account sending is paused", code="account_paused")
    if policy.mode != "production" or not policy.production_enabled:
        raise PolicyViolation(
            "Account is in test-only mode. Use controlled tests or enable production after readiness checks.",
            code="account_test_only",
        )

    from_email = str(payload.from_email or account.mailbox_email)
    await policy_service.assert_sender_allowed(account, from_email)

    suppression = await active_suppression(session, str(payload.to))
    if suppression is not None:
        raise PolicyViolation(
            "Recipient is suppressed and cannot be mailed",
            code="recipient_suppressed",
            details={"reason": suppression.reason},
        )

    total_bytes = len(
        ((payload.text_body or "") + (payload.html_body or "") + payload.subject).encode("utf-8")
    )
    if total_bytes > settings.max_message_bytes:
        raise ValidationError(
            f"Message content exceeds the {settings.max_message_bytes} byte limit",
            code="message_too_large",
        )

    fingerprint = request_fingerprint(
        {
            "account_id": account.id,
            "to_email": str(payload.to),
            "from_email": from_email,
            "reply_to_email": str(payload.reply_to) if payload.reply_to else None,
            "subject": payload.subject,
            "text_body": payload.text_body,
            "html_body": payload.html_body,
            "template_key": payload.template_key,
            "template_version": payload.template_version,
            "expires_at": payload.expires_at,
            "kind": "transactional",
        }
    )

    existing = await session.scalar(
        select(EmailMessage).where(
            EmailMessage.account_id == account.id,
            EmailMessage.idempotency_key == key,
        )
    )
    if existing is not None:
        if not fingerprint_matches(existing.request_fingerprint, fingerprint):
            raise Conflict(
                "Idempotency key was already used with a different request",
                code="idempotency_key_reused",
                details={"message_id": str(existing.id)},
            )
        response.status_code = 200
        response.headers["Idempotent-Replay"] = "true"
        return {"message": message_summary(existing), "replayed": True}

    now = datetime.now(timezone.utc)
    message = EmailMessage(
        account_id=account.id,
        kind="transactional",
        status="queued",
        idempotency_key=key,
        request_fingerprint=fingerprint,
        to_email=str(payload.to),
        from_email=from_email,
        reply_to_email=str(payload.reply_to) if payload.reply_to else None,
        subject=payload.subject,
        text_body=payload.text_body,
        html_body=payload.html_body,
        template_key=payload.template_key,
        template_version=payload.template_version,
        scheduled_at=now,
        expires_at=payload.expires_at,
    )
    session.add(message)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(EmailMessage).where(
                EmailMessage.account_id == account.id,
                EmailMessage.idempotency_key == key,
            )
        )
        if existing is not None and fingerprint_matches(existing.request_fingerprint, fingerprint):
            response.status_code = 200
            response.headers["Idempotent-Replay"] = "true"
            return {"message": message_summary(existing), "replayed": True}
        raise Conflict(
            "Concurrent request reused the same idempotency key with a different payload",
            code="idempotency_key_reused",
        )

    await record_audit(
        session,
        actor=principal.actor,
        action="message.queue",
        target_type="email_message",
        target_id=message.id,
        details={
            "account_id": str(account.id),
            "to": message.to_email,
            "kind": message.kind,
            "expires_at": iso(message.expires_at),
        },
        **_request_context(request, principal),
    )
    await session.commit()

    return {
        "message": message_summary(message),
        "replayed": False,
        "note": "Queued. Provider acceptance will be recorded as 'submitted', not 'delivered'.",
    }


@router.get("")
async def list_messages(
    account_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    statement = select(EmailMessage).order_by(EmailMessage.created_at.desc(), EmailMessage.id.desc())
    if account_id is not None:
        statement = statement.where(EmailMessage.account_id == account_id)
    if status:
        statement = statement.where(EmailMessage.status == status)
    if kind:
        statement = statement.where(EmailMessage.kind == kind)

    rows = list(await session.scalars(statement.offset(offset).limit(limit + 1)))
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "messages": [message_summary(row) for row in rows],
        "has_more": has_more,
        "next_offset": offset + len(rows) if has_more else None,
    }


@router.get("/{message_id}")
async def get_message(
    message_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    message = await session.get(EmailMessage, message_id)
    if message is None:
        raise NotFound("Message not found")

    attempts = list(
        await session.scalars(
            select(DeliveryAttempt)
            .where(DeliveryAttempt.message_id == message.id)
            .order_by(DeliveryAttempt.attempt_number)
        )
    )
    events = await recent_events(session, message_id=message.id)
    diagnostics = list(
        await session.scalars(
            select(DiagnosticReport)
            .where(DiagnosticReport.message_id == message.id)
            .order_by(DiagnosticReport.created_at.desc())
            .limit(10)
        )
    )
    return message_detail(
        message, attempts=attempts, events=events, diagnostics=diagnostics
    )


@router.post("/{message_id}/cancel")
async def cancel_message(
    message_id: uuid.UUID,
    payload: CancelRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    message = await session.get(EmailMessage, message_id)
    if message is None:
        raise NotFound("Message not found")
    if message.status not in {"queued", "retry_wait"}:
        raise Conflict(
            "Only queued or retry-wait messages can be cancelled",
            code="message_not_cancellable",
            details={"status": message.status},
        )

    message.status = "cancelled"
    message.final_error_code = "cancelled_by_operator"
    message.final_error_summary = payload.reason
    message.next_attempt_at = None
    message.updated_at = utcnow()
    await policy_service.release_quota(
        session, message_id=message.id, reason="cancelled", now=utcnow()
    )

    await record_audit(
        session,
        actor=principal.actor,
        action="message.cancel",
        target_type="email_message",
        target_id=message.id,
        details={"reason": payload.reason},
        **_request_context(request, principal),
    )
    await session.commit()
    return {"message": message_summary(message)}


@router.post("/{message_id}/requeue")
async def requeue_message(
    message_id: uuid.UUID,
    payload: RequeueRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    message = await session.get(EmailMessage, message_id, with_for_update=True)
    if message is None:
        raise NotFound("Message not found")
    if message.status not in {"failed", "needs_review", "cancelled", "expired"}:
        raise Conflict(
            "Only failed, needs_review, cancelled, or expired messages can be requeued",
            code="message_not_requeueable",
            details={"status": message.status},
        )
    if message.expires_at is not None and message.expires_at <= utcnow():
        raise Conflict("Message has expired; create a new message", code="message_expired")

    previous_status = message.status
    max_attempt = await session.scalar(
        select(func.max(DeliveryAttempt.attempt_number)).where(
            DeliveryAttempt.message_id == message.id
        )
    )
    message.status = "queued"
    # A requeue starts a fresh retry budget, but durable attempt rows must keep
    # increasing numbers so the (message_id, attempt_number) unique key holds.
    message.attempt_offset = max(message.attempt_offset, int(max_attempt or 0))
    message.attempt_count = 0
    message.final_error_code = None
    message.final_error_summary = None
    message.next_attempt_at = None
    message.scheduled_at = utcnow()
    message.lease_owner = None
    message.lease_expires_at = None
    message.updated_at = utcnow()

    await record_audit(
        session,
        actor=principal.actor,
        action="message.requeue",
        target_type="email_message",
        target_id=message.id,
        details={"from": previous_status, "note": payload.note},
        **_request_context(request, principal),
    )
    await record_safety_event(
        session,
        severity="warning",
        event_type="message_requeued_by_operator",
        action_taken="message may be submitted a second time",
        account_id=message.account_id,
        message_id=message.id,
        details={"from": previous_status, "note": payload.note, "actor": principal.actor},
    )
    await session.commit()
    return {
        "message": message_summary(message),
        "note": "The operator confirmed the resubmission risk; quota and policy apply again.",
    }


@router.post("/{message_id}/diagnostics")
async def generate_diagnostics(
    message_id: uuid.UUID,
    payload: DiagnosisRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    message = await session.get(EmailMessage, message_id)
    if message is None:
        raise NotFound("Message not found")

    report = await diagnose_message(
        session,
        message_id=message.id,
        account_id=message.account_id,
        use_llm=payload.use_llm,
        actor=principal.actor,
    )
    await session.commit()
    return {
        "diagnosis": diagnostic_payload(report),
        "advisory_only": True,
        "note": "Diagnostics never change account state, send messages, or open circuits.",
    }
