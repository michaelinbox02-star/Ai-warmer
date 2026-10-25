"""Durable queue: leasing, crash recovery, and housekeeping.

Uses `FOR UPDATE SKIP LOCKED` with expiring leases. A database row lock is the
only coordination primitive, so multiple worker processes are safe.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Sequence

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import ensure_utc, utcnow
from app.models import DeliveryAttempt, EmailAccount, EmailMessage, QuotaReservation, SendPolicy
from app.services import policy as policy_service
from app.services.audit import record_safety_event
from app.services.finalize import (
    abandon_ambiguous_lease,
    finalize_submission,
    result_from_attempt,
)

logger = logging.getLogger(__name__)

LEASE_SQL = text(
    """
    WITH candidate AS (
        SELECT m.id AS id
        FROM email_messages m
        JOIN send_policies p ON p.account_id = m.account_id
        JOIN email_accounts a ON a.id = m.account_id
        WHERE m.status IN ('queued', 'retry_wait')
          AND m.scheduled_at <= :now
          AND (m.next_attempt_at IS NULL OR m.next_attempt_at <= :now)
          AND (m.expires_at IS NULL OR m.expires_at > :now)
          AND p.mode <> 'paused'
          AND p.circuit_state IN ('closed', 'half_open')
          AND a.state = 'connected'
          AND (p.mode = 'production' OR m.kind = 'diagnostic_test')
          AND (
              SELECT count(*) FROM quota_reservations q
              WHERE q.account_id = m.account_id AND q.status = 'reserved'
          ) < p.max_in_flight
        ORDER BY COALESCE(m.next_attempt_at, m.scheduled_at), m.created_at
        FOR UPDATE OF m SKIP LOCKED
        LIMIT 1
    )
    UPDATE email_messages m
    SET status = 'sending',
        lease_owner = :worker_id,
        lease_expires_at = :lease_until,
        attempt_count = m.attempt_count + 1,
        updated_at = :now
    FROM candidate
    WHERE m.id = candidate.id
    RETURNING m.id
    """
)


async def lease_next(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> EmailMessage | None:
    moment = ensure_utc(now) or utcnow()
    result = await session.execute(
        LEASE_SQL,
        {
            "now": moment,
            "lease_until": moment + timedelta(seconds=max(30, lease_seconds)),
            "worker_id": worker_id,
        },
    )
    row = result.first()
    if row is None:
        return None
    message = await session.get(EmailMessage, row[0], populate_existing=True)
    return message


async def defer_without_attempt(
    session: AsyncSession,
    *,
    message: EmailMessage,
    delay_seconds: int,
    reason: str,
    now: datetime | None = None,
) -> None:
    """Return a leased message to the queue without counting an attempt.

    Used when policy or quota blocks the send before any provider I/O.
    """

    moment = ensure_utc(now) or utcnow()
    attempt = await session.scalar(
        select(DeliveryAttempt).where(
            DeliveryAttempt.message_id == message.id,
            DeliveryAttempt.attempt_number == message.attempt_offset + message.attempt_count,
        )
    )
    if attempt is not None and attempt.finished_at is None:
        # No provider call happened; remove the provisional marker.
        await session.delete(attempt)

    message.attempt_count = max(0, int(message.attempt_count) - 1)
    message.status = "retry_wait"
    message.next_attempt_at = moment + timedelta(seconds=max(1, delay_seconds))
    message.lease_owner = None
    message.lease_expires_at = None
    message.updated_at = moment

    await record_safety_event(
        session,
        severity="info",
        event_type="submission_deferred",
        action_taken=f"submission deferred for {delay_seconds}s",
        message_id=message.id,
        details={"reason": reason},
    )


async def recover_expired_leases(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 20,
) -> list[uuid.UUID]:
    """Recover messages whose worker died mid-flight.

    - no attempt row: the worker never contacted the provider → safe requeue;
    - provisional attempt row: the provider may have received the message →
      `needs_review` (never auto-resubmit);
    - finished attempt row: replay the recorded outcome through finalize.
    """

    moment = ensure_utc(now) or utcnow()
    ids = (
        (
            await session.execute(
                text(
                    """
                    SELECT id FROM email_messages
                    WHERE status = 'sending'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at < :now
                    ORDER BY lease_expires_at
                    LIMIT :limit
                    FOR UPDATE SKIP LOCKED
                    """
                ),
                {"now": moment, "limit": limit},
            )
        )
        .scalars()
        .all()
    )

    recovered: list[uuid.UUID] = []
    for message_id in ids:
        message = await session.get(EmailMessage, message_id, populate_existing=True)
        if message is None:
            continue
        account = await session.get(EmailAccount, message.account_id)
        policy = await session.get(SendPolicy, message.account_id)
        if account is None or policy is None:
            continue

        attempt = await session.scalar(
            select(DeliveryAttempt).where(
                DeliveryAttempt.message_id == message.id,
                DeliveryAttempt.attempt_number
                == message.attempt_offset + message.attempt_count,
            )
        )

        if attempt is None:
            message.status = "queued"
            message.attempt_count = max(0, int(message.attempt_count) - 1)
            message.lease_owner = None
            message.lease_expires_at = None
            message.next_attempt_at = moment
            message.updated_at = moment
            await record_safety_event(
                session,
                severity="info",
                event_type="lease_recovered",
                action_taken="message returned to the queue",
                account_id=account.id,
                message_id=message.id,
                details={"reason": "worker lease expired before provider contact"},
            )
        elif attempt.finished_at is None:
            await abandon_ambiguous_lease(
                session,
                message=message,
                account=account,
                reason="worker lease expired while a submission was in progress",
            )
        else:
            await finalize_submission(
                session,
                message=message,
                account=account,
                policy=policy,
                result=result_from_attempt(attempt),
                now=moment,
            )
        recovered.append(message.id)

    return recovered


async def expire_messages(session: AsyncSession, *, now: datetime | None = None) -> int:
    moment = ensure_utc(now) or utcnow()
    rows = (
        await session.execute(
            select(EmailMessage)
            .where(
                EmailMessage.status.in_(("queued", "retry_wait")),
                EmailMessage.expires_at.is_not(None),
                EmailMessage.expires_at <= moment,
            )
            .with_for_update(skip_locked=True)
        )
    ).scalars()

    count = 0
    for message in rows:
        message.status = "expired"
        message.final_error_code = "expired"
        message.final_error_summary = "Message expired before it could be submitted."
        message.next_attempt_at = None
        message.updated_at = moment
        await policy_service.release_quota(
            session, message_id=message.id, reason="expired", now=moment
        )
        count += 1
    return count


async def reopen_circuits(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Move auto-open circuits to half-open once their window elapses.

    Circuits opened for authentication, quota, TLS, or complaint reasons carry
    the manual-reset marker and are never reopened automatically.
    """

    moment = ensure_utc(now) or utcnow()
    policies = (
        await session.execute(
            select(SendPolicy).where(
                SendPolicy.circuit_state == "open",
                SendPolicy.circuit_open_until.is_not(None),
                SendPolicy.circuit_open_until <= moment,
            )
        )
    ).scalars()

    count = 0
    for policy in policies:
        if policy_service.circuit_requires_manual_reset(policy):
            continue
        policy.circuit_state = "half_open"
        policy.updated_at = moment
        await record_safety_event(
            session,
            severity="info",
            event_type="circuit_half_open",
            action_taken="one low-risk probe permitted",
            account_id=policy.account_id,
            details={"reason": policy.circuit_reason},
        )
        count += 1
    return count


async def release_orphan_reservations(
    session: AsyncSession, *, now: datetime | None = None, older_than_minutes: int = 15
) -> int:
    """Settle reservations left behind by a crashed worker.

    A reservation attached to a message that shows any sign of provider
    acceptance is consumed, not released, so an ambiguous crash cannot increase
    the effective send rate. Reservations for messages that never left the
    queue are released.
    """

    moment = ensure_utc(now) or utcnow()
    cutoff = moment - timedelta(minutes=older_than_minutes)
    rows = (
        await session.execute(
            select(QuotaReservation, EmailMessage.status)
            .join(EmailMessage, EmailMessage.id == QuotaReservation.message_id)
            .where(
                QuotaReservation.status == "reserved",
                QuotaReservation.reserved_at < cutoff,
                EmailMessage.status != "sending",
            )
            .with_for_update(skip_locked=True)
        )
    ).all()

    accepted = {"submitted", "delivered", "bounced", "complained", "needs_review"}
    count = 0
    for reservation, message_status in rows:
        if message_status in accepted:
            reservation.status = "consumed"
            reservation.consumed_at = moment
        else:
            reservation.status = "released"
            reservation.released_at = moment
            reservation.release_reason = "stale_reservation"
        count += 1
    return count


async def housekeeping(session: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    moment = ensure_utc(now) or utcnow()
    expired = await expire_messages(session, now=moment)
    reopened = await reopen_circuits(session, now=moment)
    released = await release_orphan_reservations(session, now=moment)
    return {"expired": expired, "circuits_reopened": reopened, "reservations_released": released}


async def backlog_snapshot(session: AsyncSession) -> dict[str, object]:
    row = (
        await session.execute(
            text(
                """
                SELECT
                    count(*) FILTER (WHERE status IN ('queued', 'retry_wait')) AS queued,
                    count(*) FILTER (WHERE status = 'sending') AS sending,
                    count(*) FILTER (WHERE status = 'needs_review') AS needs_review,
                    min(COALESCE(next_attempt_at, scheduled_at))
                        FILTER (WHERE status IN ('queued', 'retry_wait')) AS oldest_due,
                    count(*) FILTER (
                        WHERE status = 'sending' AND lease_expires_at < now()
                    ) AS expired_leases
                FROM email_messages
                """
            )
        )
    ).one()
    return {
        "queued": int(row.queued or 0),
        "sending": int(row.sending or 0),
        "needs_review": int(row.needs_review or 0),
        "oldest_due": row.oldest_due.isoformat() if row.oldest_due else None,
        "expired_leases": int(row.expired_leases or 0),
    }
