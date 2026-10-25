"""Applying provider outcomes to durable state.

This is the only place that transitions a message out of `sending`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.policy_rules import (
    CircuitTrigger,
    decide_outcome,
    circuit_after_failure,
    circuit_after_success,
)
from app.core.time import ensure_utc, utcnow
from app.models import (
    DeliveryAttempt,
    EmailAccount,
    EmailMessage,
    SendPolicy,
)
from app.providers.base import ProviderSubmissionResult
from app.services import policy as policy_service
from app.services.audit import record_safety_event
from app.services.credentials import persist_token_refresh

_RETRYABILITY_BY_OUTCOME = {
    "accepted": "transient",  # unused
    "transient_failure": "transient",
    "permanent_failure": "permanent",
    "auth_failure": "auth",
    "rate_limited": "rate_limited",
    "unknown_outcome": "unknown",
}


async def record_attempt_started(
    session: AsyncSession,
    *,
    message: EmailMessage,
    provider: str,
    started_at: datetime,
) -> DeliveryAttempt:
    """Write a provisional attempt row before external I/O.

    The row starts as `unknown_outcome` (allowed by the schema) and is the
    crash marker that tells lease recovery whether the provider may have been
    contacted.
    """

    attempt_number = message.attempt_offset + message.attempt_count
    existing = await session.scalar(
        select(DeliveryAttempt).where(
            DeliveryAttempt.message_id == message.id,
            DeliveryAttempt.attempt_number == attempt_number,
        )
    )
    if existing is not None:
        return existing

    attempt = DeliveryAttempt(
        message_id=message.id,
        attempt_number=attempt_number,
        provider=provider,
        outcome="unknown_outcome",
        started_at=started_at,
        response_metadata={"provisional": True},
    )
    session.add(attempt)
    await session.flush()
    return attempt


def result_from_attempt(attempt: DeliveryAttempt) -> ProviderSubmissionResult:
    """Reconstruct a submission result from a recorded attempt (crash recovery)."""

    outcome = attempt.outcome
    retryability = _RETRYABILITY_BY_OUTCOME.get(outcome, "unknown")
    return ProviderSubmissionResult(
        accepted=outcome == "accepted",
        retryability=retryability,  # type: ignore[arg-type]
        submitted_at=attempt.finished_at or attempt.started_at,
        provider_message_id=attempt.provider_message_id,
        http_status=attempt.http_status,
        smtp_code=attempt.smtp_code,
        enhanced_smtp_code=attempt.enhanced_smtp_code,
        diagnostic_code=attempt.diagnostic_code,
        detail=attempt.sanitized_detail,
        retry_after_seconds=attempt.retry_after_seconds,
        metadata=dict(attempt.response_metadata or {}),
    )


async def _consecutive_transient_failures(
    session: AsyncSession, account_id: uuid.UUID, *, limit: int = 8
) -> int:
    rows = (
        await session.execute(
            select(DeliveryAttempt.outcome)
            .join(EmailMessage, EmailMessage.id == DeliveryAttempt.message_id)
            .where(
                EmailMessage.account_id == account_id,
                DeliveryAttempt.finished_at.is_not(None),
            )
            .order_by(DeliveryAttempt.started_at.desc(), DeliveryAttempt.attempt_number.desc())
            .limit(limit)
        )
    ).scalars()
    streak = 0
    for outcome in rows:
        if outcome == "transient_failure":
            streak += 1
        else:
            break
    return streak


async def finalize_submission(
    session: AsyncSession,
    *,
    message: EmailMessage,
    account: EmailAccount,
    policy: SendPolicy,
    result: ProviderSubmissionResult,
    now: datetime | None = None,
) -> None:
    moment = ensure_utc(now) or utcnow()
    if message.status != "sending":
        return

    attempt_number = message.attempt_offset + message.attempt_count
    attempt = await session.scalar(
        select(DeliveryAttempt)
        .where(
            DeliveryAttempt.message_id == message.id,
            DeliveryAttempt.attempt_number == attempt_number,
        )
        .with_for_update()
    )
    if attempt is None:
        attempt = DeliveryAttempt(
            message_id=message.id,
            attempt_number=attempt_number,
            provider=account.transport,
            outcome=result.outcome,
            started_at=moment,
        )
        session.add(attempt)
        await session.flush()

    duration_ms = None
    started_at = ensure_utc(attempt.started_at)
    if started_at is not None:
        duration_ms = max(0, int((moment - started_at).total_seconds() * 1000))

    attempt.outcome = result.outcome
    attempt.finished_at = moment
    attempt.duration_ms = duration_ms
    attempt.http_status = result.http_status
    attempt.smtp_code = result.smtp_code
    attempt.enhanced_smtp_code = result.enhanced_smtp_code
    attempt.provider_message_id = result.provider_message_id
    attempt.retry_after_seconds = result.retry_after_seconds
    attempt.diagnostic_code = result.diagnostic_code
    attempt.sanitized_detail = result.detail
    attempt.response_metadata = {**result.metadata, "provisional": False}

    message.lease_owner = None
    message.lease_expires_at = None
    message.updated_at = moment

    await persist_token_refresh(
        session, account_id=account.id, update=result.credentials_update
    )

    if result.accepted:
        message.status = "submitted"
        message.provider_message_id = result.provider_message_id
        message.submitted_at = result.submitted_at
        message.next_attempt_at = None
        message.final_error_code = None
        message.final_error_summary = None
        await policy_service.consume_quota(session, message_id=message.id, now=moment)
        account.last_error_code = None
        account.last_error_summary = None
        account.updated_at = moment

        # Delivery evidence may have arrived while the submission was in flight.
        if message.delivery_evidence_at is not None:
            message.status = "delivered"

        if policy.circuit_state == "half_open":
            decision = circuit_after_success(policy.circuit_state)  # type: ignore[arg-type]
            if decision is not None:
                policy.circuit_state = "closed"
                policy.circuit_reason = None
                policy.circuit_open_until = None
                policy.updated_at = moment
                await record_safety_event(
                    session,
                    severity="info",
                    event_type="circuit_closed",
                    action_taken="probe succeeded; sending permitted again",
                    account_id=account.id,
                    message_id=message.id,
                )
        return

    decision = decide_outcome(
        result.retryability,
        attempt_count=message.attempt_count,
        max_attempts=policy.max_attempts,
        now=moment,
        retry_after_seconds=result.retry_after_seconds,
        expires_at=ensure_utc(message.expires_at),
    )

    message.final_error_code = result.diagnostic_code
    message.final_error_summary = result.detail
    account.last_error_code = result.diagnostic_code
    account.last_error_summary = result.detail
    account.updated_at = moment

    if result.retryability == "auth":
        # Credentials were rejected before acceptance; releasing the slot is
        # correct. The account is paused and the circuit requires review.
        account.state = "auth_required"
        message.status = "failed"
        message.next_attempt_at = None
        await policy_service.open_circuit(
            session,
            policy=policy,
            account=account,
            reason=result.detail or "provider rejected credentials",
            trigger="auth_failure",
            requires_manual=True,
            message_id=message.id,
        )
        await record_safety_event(
            session,
            severity="critical",
            event_type="credentials_rejected",
            action_taken="account paused, circuit opened, message failed",
            account_id=account.id,
            message_id=message.id,
            details={"diagnostic_code": result.diagnostic_code},
        )
        return

    if result.retryability == "unknown":
        # The provider may have accepted the message. Keep the slot consumed
        # so an ambiguous outcome can never increase the effective send rate.
        await policy_service.consume_quota(
            session, message_id=message.id, now=moment
        )
        message.status = "needs_review"
        message.next_attempt_at = None
        await record_safety_event(
            session,
            severity="warning",
            event_type="ambiguous_submission",
            action_taken="automatic resubmission blocked; operator review required",
            account_id=account.id,
            message_id=message.id,
            details={
                "diagnostic_code": result.diagnostic_code,
                "detail": result.detail,
                "attempt": message.attempt_count,
            },
        )
        return

    # Every remaining outcome is a confirmed rejection, so the quota slot is
    # released and the message may be retried or failed.
    await policy_service.release_quota(
        session, message_id=message.id, reason=result.outcome, now=moment
    )

    if result.circuit_trigger == "quota_restriction":
        await policy_service.open_circuit(
            session,
            policy=policy,
            account=account,
            reason=result.detail or "provider quota or rate restriction",
            trigger="quota_restriction",
            requires_manual=True,
            message_id=message.id,
        )

    if result.circuit_trigger == "tls_failure":
        await policy_service.open_circuit(
            session,
            policy=policy,
            account=account,
            reason=result.detail or "TLS validation failed",
            trigger="tls_failure",
            requires_manual=True,
            message_id=message.id,
        )

    if decision.action == "retry":
        message.status = "retry_wait"
        message.next_attempt_at = decision.retry_at
        await record_safety_event(
            session,
            severity="info",
            event_type="submission_retry_scheduled",
            action_taken=f"retry scheduled at {decision.retry_at.isoformat() if decision.retry_at else 'unknown'}",
            account_id=account.id,
            message_id=message.id,
            details={
                "retryability": result.retryability,
                "attempt": message.attempt_count,
                "max_attempts": policy.max_attempts,
            },
        )
        streak = await _consecutive_transient_failures(session, account.id)
        circuit = circuit_after_failure(
            CircuitTrigger.TRANSIENT_STREAK, now=moment, consecutive_transient=streak
        )
        if circuit is not None and policy.circuit_state == "closed":
            await policy_service.open_circuit(
                session,
                policy=policy,
                account=account,
                reason=circuit.reason,
                trigger="transient_streak",
                requires_manual=False,
                open_until=circuit.open_until,
                message_id=message.id,
            )
        return

    message.status = "failed"
    message.next_attempt_at = None
    await record_safety_event(
        session,
        severity="warning",
        event_type="submission_failed",
        action_taken="message will not be retried",
        account_id=account.id,
        message_id=message.id,
        details={
            "retryability": result.retryability,
            "reason": decision.reason,
            "attempts": message.attempt_count,
            "max_attempts": policy.max_attempts,
        },
    )


async def abandon_ambiguous_lease(
    session: AsyncSession,
    *,
    message: EmailMessage,
    account: EmailAccount,
    reason: str,
) -> None:
    """A lease expired while the provider may have received the message."""

    message.status = "needs_review"
    message.lease_owner = None
    message.lease_expires_at = None
    message.next_attempt_at = None
    message.updated_at = utcnow()
    # The provider may have accepted this message. Consume the slot rather
    # than releasing it so an ambiguous outcome cannot raise the send rate.
    await policy_service.consume_quota(session, message_id=message.id, now=utcnow())
    await record_safety_event(
        session,
        severity="critical",
        event_type="ambiguous_submission",
        action_taken="automatic resubmission blocked; operator review required",
        account_id=account.id,
        message_id=message.id,
        details={"reason": reason, "attempt": message.attempt_count},
    )
