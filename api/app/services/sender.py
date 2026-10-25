"""The sender worker.

Safety ordering per message:

1. lease the row (`FOR UPDATE SKIP LOCKED`) and re-check policy;
2. re-check suppression and expiry;
3. reserve quota under a policy row lock, then commit the claim;
4. write a provisional attempt marker, then commit;
5. call the provider outside any database transaction;
6. finalize in a new transaction (accepted → submitted, never delivered).

A crash after step 4 is deliberately not auto-retried: the provider may have
accepted the message.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import PolicyViolation
from app.core.redaction import sanitize_text
from app.core.time import ensure_utc, utcnow
from app.models import DeliveryAttempt, EmailAccount, EmailMessage, SendPolicy
from app.providers.base import OutboundMessage, ProviderSubmissionResult
from app.providers.registry import provider_for_transport
from app.services import policy as policy_service
from app.services import queue as queue_service
from app.services.audit import record_safety_event
from app.services.credentials import build_context
from app.services.finalize import finalize_submission, record_attempt_started
from app.services.suppression import active_suppression

logger = logging.getLogger(__name__)


class SenderService:
    def __init__(
        self,
        *,
        worker_id: str | None = None,
        lease_seconds: int | None = None,
        send_timeout_seconds: int | None = None,
    ) -> None:
        self.worker_id = worker_id or settings.worker_id
        self.lease_seconds = lease_seconds or settings.worker_lease_seconds
        self.send_timeout = send_timeout_seconds or settings.worker_send_timeout_seconds

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------
    async def run_forever(self, stop: asyncio.Event) -> None:
        logger.info("Sender worker %s started", self.worker_id)
        last_housekeeping = 0.0
        while not stop.is_set():
            worked = False
            try:
                async with _session() as session:
                    worked = await self.run_once(session)
            except Exception:  # noqa: BLE001 - keep the worker alive
                logger.exception("Worker iteration failed")
                await asyncio.sleep(2)

            now_monotonic = asyncio.get_running_loop().time()
            if now_monotonic - last_housekeeping >= settings.worker_housekeeping_seconds:
                last_housekeeping = now_monotonic
                try:
                    await self.housekeeping()
                except Exception:  # noqa: BLE001
                    logger.exception("Housekeeping failed")

            if not worked:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
                except asyncio.TimeoutError:
                    pass

        logger.info("Sender worker %s stopped", self.worker_id)

    async def run_once(self, session: AsyncSession) -> bool:
        message = await queue_service.lease_next(
            session, worker_id=self.worker_id, lease_seconds=self.lease_seconds
        )
        if message is None:
            await session.rollback()
            return False

        message_id = message.id
        try:
            await self.process_lease(session, message)
        except Exception:  # noqa: BLE001 - finalize below handles state
            logger.exception("Failed to process message %s", message_id)
            await session.rollback()
            await self._mark_ambiguous(message_id, "worker error during submission")
        return True

    async def housekeeping(self) -> dict[str, int]:
        async with _session() as session:
            recovered = await queue_service.recover_expired_leases(session)
            counts = await queue_service.housekeeping(session)
            await session.commit()
        if recovered:
            logger.warning("Recovered %d expired leases", len(recovered))
        return counts

    # ------------------------------------------------------------------
    # Per-message pipeline
    # ------------------------------------------------------------------
    async def process_lease(self, session: AsyncSession, message: EmailMessage) -> None:
        message_id = message.id
        account = await session.get(EmailAccount, message.account_id)
        policy = await session.get(SendPolicy, message.account_id)
        if account is None or policy is None:
            await session.rollback()
            await self._terminal_without_attempt(
                message_id, "failed", "account_missing", "Account or policy no longer exists"
            )
            return

        # Phase 1: preflight inside the lease transaction.
        try:
            await self._preflight(session, message=message, account=account, policy=policy)
            await policy_service.reserve_quota(
                session, account_id=account.id, message_id=message.id
            )
            await session.commit()
        except policy_service.QuotaExceeded as exc:
            await session.rollback()
            retry_after = int(exc.details.get("retry_after_seconds", 30))
            await self._requeue(message_id, retry_after, exc.code)
            return
        except PolicyViolation as exc:
            await session.rollback()
            if exc.code in {
                "global_sending_paused",
                "circuit_open",
                "account_paused",
                "account_test_only",
                "account_auth_required",
                "account_pending",
                "account_error",
            }:
                await self._requeue(message_id, 60, exc.code)
            elif exc.code == "recipient_suppressed":
                await self._terminal_without_attempt(
                    message_id, "cancelled", exc.code, exc.message
                )
            else:
                status = "expired" if exc.code == "message_expired" else "failed"
                await self._terminal_without_attempt(
                    message_id, status, exc.code, exc.message
                )
            return
        except Exception:
            await session.rollback()
            raise

        # Phase 2: durable crash marker, committed before external I/O.
        started_at = utcnow()
        try:
            refreshed = await session.get(EmailMessage, message_id, populate_existing=True)
            if refreshed is None or refreshed.status != "sending":
                await session.rollback()
                return
            await record_attempt_started(
                session, message=refreshed, provider=account.transport, started_at=started_at
            )
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("Could not record attempt marker for %s", message_id)
            return

        # Phase 3: provider call, no database transaction held.
        result = await self._submit(session, message_id=message_id, account=account)

        # Phase 4: finalize.
        await self._finalize(session, message_id=message_id, result=result, now=utcnow())

    async def _preflight(
        self,
        session: AsyncSession,
        *,
        message: EmailMessage,
        account: EmailAccount,
        policy: SendPolicy,
    ) -> None:
        moment = utcnow()
        if message.expires_at is not None and ensure_utc(message.expires_at) <= moment:
            raise PolicyViolation("Message has expired", code="message_expired")

        suppression = await active_suppression(session, message.to_email)
        if suppression is not None:
            raise PolicyViolation(
                f"Recipient is suppressed ({suppression.reason})",
                code="recipient_suppressed",
            )
        await policy_service.assert_send_allowed(
            session, account=account, policy=policy, kind=message.kind, now=moment
        )

    async def _submit(
        self, session: AsyncSession, *, message_id: uuid.UUID, account: EmailAccount
    ) -> ProviderSubmissionResult:
        submitted_at = utcnow()
        try:
            message = await session.get(EmailMessage, message_id, populate_existing=True)
            if message is None:
                raise RuntimeError("message disappeared")
            ctx = await build_context(session, account)
            provider = provider_for_transport(account.transport)

            outbound = OutboundMessage(
                message_id=message.id,
                from_email=message.from_email,
                from_name=account.display_name,
                to_email=message.to_email,
                reply_to_email=message.reply_to_email,
                subject=message.subject,
                text_body=message.text_body,
                html_body=message.html_body,
                kind=message.kind,
                created_at=message.created_at,
            )
            # Release the read transaction before doing network I/O.
            await session.rollback()
            return await asyncio.wait_for(
                provider.send(ctx, outbound), timeout=self.send_timeout
            )
        except asyncio.TimeoutError:
            logger.warning("Provider call timed out for message %s", message_id)
            return ProviderSubmissionResult(
                accepted=False,
                retryability="unknown",
                submitted_at=submitted_at,
                diagnostic_code="provider_timeout",
                detail="Provider call timed out; the message may or may not have been accepted.",
                metadata={"stage": "timeout"},
            )
        except PolicyViolation as exc:
            return ProviderSubmissionResult(
                accepted=False,
                retryability="permanent",
                submitted_at=submitted_at,
                diagnostic_code=exc.code,
                detail=exc.message,
            )
        except Exception as exc:  # noqa: BLE001 - never retry an unknown adapter failure blindly
            logger.exception("Provider adapter raised for message %s", message_id)
            return ProviderSubmissionResult(
                accepted=False,
                retryability="unknown",
                submitted_at=submitted_at,
                diagnostic_code="adapter_error",
                detail=sanitize_text(f"{type(exc).__name__}: {exc}", limit=300),
                metadata={"stage": "adapter"},
            )

    async def _finalize(
        self,
        session: AsyncSession,
        *,
        message_id: uuid.UUID,
        result: ProviderSubmissionResult,
        now: datetime,
    ) -> None:
        for attempt in range(2):
            try:
                message = await session.get(
                    EmailMessage, message_id, populate_existing=True, with_for_update=True
                )
                if message is None or message.status != "sending":
                    await session.rollback()
                    return
                if message.lease_owner not in (None, self.worker_id):
                    await session.rollback()
                    logger.warning(
                        "Message %s was recovered by another worker; dropping local result",
                        message_id,
                    )
                    return
                account = await session.get(EmailAccount, message.account_id)
                policy = await session.get(SendPolicy, message.account_id)
                if account is None or policy is None:
                    await session.rollback()
                    return
                await finalize_submission(
                    session,
                    message=message,
                    account=account,
                    policy=policy,
                    result=result,
                    now=now,
                )
                from app.services.test_runs import on_message_state_change

                await on_message_state_change(session, message)
                await session.commit()
                return
            except Exception:
                await session.rollback()
                if attempt == 0:
                    logger.exception("Finalize failed for %s; retrying once", message_id)
                    continue
                logger.exception("Finalize failed for %s; marking ambiguous", message_id)
                await self._mark_ambiguous(
                    message_id,
                    "finalize failure",
                    provider_accepted=result.accepted,
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _requeue(self, message_id: uuid.UUID, delay_seconds: int, reason: str) -> None:
        async with _session() as session:
            message = await session.get(EmailMessage, message_id, populate_existing=True)
            if message is None or message.status not in ("sending", "queued", "retry_wait"):
                await session.rollback()
                return
            now = utcnow()
            await self._undo_lease_attempt(session, message)
            message.status = "queued"
            message.next_attempt_at = now + timedelta(seconds=max(1, delay_seconds))
            message.lease_owner = None
            message.lease_expires_at = None
            message.updated_at = now
            await record_safety_event(
                session,
                severity="info",
                event_type="submission_deferred",
                action_taken=f"submission deferred for {delay_seconds}s",
                account_id=message.account_id,
                message_id=message.id,
                details={"reason": reason},
            )
            await session.commit()

    @staticmethod
    async def _undo_lease_attempt(session: AsyncSession, message: EmailMessage) -> None:
        """Undo the lease's attempt increment when no provider I/O happened.

        Leasing increments ``attempt_count`` so a crash leaves evidence. If a
        policy/quota preflight rejects the message, no provider was contacted
        and the increment must not count against the retry budget.
        """

        attempt = await session.scalar(
            select(DeliveryAttempt).where(
                DeliveryAttempt.message_id == message.id,
                DeliveryAttempt.attempt_number
                == message.attempt_offset + message.attempt_count,
            )
        )
        if attempt is None:
            message.attempt_count = max(0, int(message.attempt_count) - 1)

    async def _terminal_without_attempt(
        self, message_id: uuid.UUID, status: str, code: str, summary: str
    ) -> None:
        async with _session() as session:
            message = await session.get(EmailMessage, message_id, populate_existing=True)
            if message is None or message.status not in ("sending", "queued", "retry_wait"):
                await session.rollback()
                return
            await self._undo_lease_attempt(session, message)
            message.status = status
            message.final_error_code = code
            message.final_error_summary = sanitize_text(summary, limit=300)
            message.next_attempt_at = None
            message.lease_owner = None
            message.lease_expires_at = None
            message.updated_at = utcnow()
            await session.commit()

    async def _mark_ambiguous(
        self,
        message_id: uuid.UUID,
        reason: str,
        *,
        provider_accepted: bool = False,
    ) -> None:
        async with _session() as session:
            message = await session.get(EmailMessage, message_id, populate_existing=True)
            if message is None or message.status != "sending":
                await session.rollback()
                return
            if provider_accepted:
                # The provider accepted but we could not persist the final
                # state. Keep the quota slot consumed so a database failure
                # cannot silently increase the effective sending rate.
                await policy_service.consume_quota(
                    session, message_id=message.id, now=utcnow()
                )

            account = await session.get(EmailAccount, message.account_id)
            if account is not None:
                from app.services.finalize import abandon_ambiguous_lease

                await abandon_ambiguous_lease(
                    session, message=message, account=account, reason=reason
                )
            else:
                message.status = "needs_review"
                message.lease_owner = None
                message.lease_expires_at = None
            if provider_accepted:
                message.final_error_summary = (
                    "Provider accepted the message, but finalize failed. "
                    "The quota slot remains consumed; do not resubmit without review."
                )
            await session.commit()


def _session() -> AsyncSession:
    from app.db import SessionLocal

    return SessionLocal()


async def recover_due_lease(
    session: AsyncSession, *, now: datetime | None = None
) -> list[uuid.UUID]:
    return await queue_service.recover_expired_leases(session, now=now)


async def count_queued(session: AsyncSession) -> int:
    from sqlalchemy import func

    return int(await session.scalar(select(func.count()).select_from(EmailMessage).where(
        EmailMessage.status.in_(("queued", "retry_wait"))
    )) or 0)
