"""Sending policy: readiness, quota reservations, and pre-send checks.

Quota reservations are taken before external I/O while holding a row lock on
the account's `send_policies` row, consumed only after the provider accepts,
and released on confirmed failure. This makes concurrent overrun impossible
even with multiple worker processes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import PolicyViolation
from app.core.policy_rules import (
    AuthPolicy,
    ReadinessReport,
    evaluate_readiness,
)
from app.core.time import ensure_utc, utcnow
from app.models import (
    AllowedTestRecipient,
    DomainAuthCheck,
    EmailAccount,
    EmailMessage,
    ProviderCredential,
    QuotaReservation,
    SendPolicy,
)
from app.services import settings as app_settings
from app.services.audit import record_audit, record_safety_event
from app.services.credentials import get_credential

MANUAL_RESET_MARKER = "manual review required"


class QuotaExceeded(PolicyViolation):
    code = "quota_exceeded"


def _domain_of(address: str | None) -> str | None:
    if not address or "@" not in address:
        return None
    return address.rsplit("@", 1)[1].strip().lower().rstrip(".") or None


def effective_auth_policy(account: EmailAccount) -> AuthPolicy:
    raw = (account.provider_metadata or {}).get("auth_policy")
    if not isinstance(raw, dict):
        return AuthPolicy()
    values: dict[str, str] = {}
    for key in ("spf", "dkim", "dmarc", "mx"):
        value = raw.get(key)
        if value in {"required", "recommended", "off"}:
            values[key] = value
    return AuthPolicy(**values)  # type: ignore[arg-type]


def sender_identity_verified(account: EmailAccount) -> bool:
    metadata = account.provider_metadata or {}
    if metadata.get("sender_identity_verified") is True:
        return True
    mailbox_domain = _domain_of(account.mailbox_email)
    sending_domain = (account.sending_domain or mailbox_domain or "").lower().rstrip(".") or None
    return bool(sending_domain and mailbox_domain and sending_domain == mailbox_domain)


async def latest_auth_statuses(
    session: AsyncSession, account_id: uuid.UUID, *, now: datetime | None = None
) -> dict[str, str]:
    moment = ensure_utc(now) or utcnow()
    rows = (
        await session.execute(
            select(DomainAuthCheck.check_type, DomainAuthCheck.status, DomainAuthCheck.checked_at)
            .where(
                DomainAuthCheck.account_id == account_id,
                (DomainAuthCheck.expires_at.is_(None)) | (DomainAuthCheck.expires_at > moment),
            )
            .order_by(DomainAuthCheck.check_type, DomainAuthCheck.checked_at.desc())
            .distinct(DomainAuthCheck.check_type)
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def _credentials_valid(session: AsyncSession, account: EmailAccount) -> bool:
    credential = await get_credential(session, account.id)
    if credential is None:
        return False
    return account.state != "auth_required" and account.last_error_code not in {
        "auth_failure",
        "smtp_auth_failed",
        "invalid_grant",
        "invalid_token",
    }


async def build_readiness(
    session: AsyncSession,
    account: EmailAccount,
    *,
    policy: SendPolicy | None = None,
    global_enabled: bool | None = None,
) -> ReadinessReport:
    policy = policy or await session.get(SendPolicy, account.id)
    if policy is None:
        raise PolicyViolation("Send policy is missing for this account", code="policy_missing")

    if global_enabled is None:
        global_enabled = await app_settings.is_global_sending_enabled(session)

    auth_statuses = await latest_auth_statuses(session, account.id)
    tls_status = auth_statuses.get("tls")
    tls_valid = True if tls_status == "pass" else (False if tls_status else None)
    acknowledged = await app_settings.acknowledged_warnings(session, account.id)

    return evaluate_readiness(
        connection_valid=account.connection_tested_at is not None and account.state == "connected",
        transport=account.transport,  # type: ignore[arg-type]
        tls_valid=tls_valid,
        sender_identity_verified=sender_identity_verified(account),
        auth_checks=auth_statuses,
        auth_policy=effective_auth_policy(account),
        global_sending_enabled=bool(global_enabled),
        account_mode=policy.mode,  # type: ignore[arg-type]
        circuit_state=policy.circuit_state,  # type: ignore[arg-type]
        credentials_valid=await _credentials_valid(session, account),
        acknowledged_warnings=acknowledged,
    )


async def enable_production(
    session: AsyncSession,
    *,
    account: EmailAccount,
    policy: SendPolicy,
    actor: str,
    acknowledgements: list[str] | None = None,
    request_id: str | None = None,
    source_ip: str | None = None,
) -> ReadinessReport:
    if acknowledgements:
        await app_settings.acknowledge_warnings(
            session,
            account_id=account.id,
            keys=acknowledgements,
            actor=actor,
            request_id=request_id,
            source_ip=source_ip,
        )

    report = await build_readiness(session, account, policy=policy)
    # `account_mode` and the global switch are configuration state, not
    # readiness evidence: an operator may pre-stage production mode while
    # global sending is paused. Both are still reported in the checklist.
    blocking = [key for key in report.blocking if key not in {"account_mode", "global_sending"}]
    if blocking:
        raise PolicyViolation(
            "Production readiness checks are not satisfied",
            code="not_ready_for_production",
            details={"blocking": blocking, "checks": report.as_dict()["checks"]},
        )

    policy.mode = "production"
    policy.production_enabled = True
    policy.updated_at = utcnow()

    await record_audit(
        session,
        actor=actor,
        action="account.enable_production",
        target_type="email_account",
        target_id=account.id,
        request_id=request_id,
        source_ip=source_ip,
        details={"limits": {"per_minute": policy.max_per_minute, "rolling_24h": policy.max_rolling_24h}},
    )
    await record_safety_event(
        session,
        severity="info",
        event_type="account_production_enabled",
        action_taken="production sending permitted within policy",
        account_id=account.id,
        details={"actor": actor},
    )
    return report


async def assert_send_allowed(
    session: AsyncSession,
    *,
    account: EmailAccount,
    policy: SendPolicy,
    kind: str,
    now: datetime | None = None,
) -> None:
    """Cheap, non-locking checks performed immediately before submission."""

    moment = ensure_utc(now) or utcnow()

    if not await app_settings.is_global_sending_enabled(session):
        raise PolicyViolation(
            "Global sending is paused. Future submissions are blocked; already accepted "
            "messages cannot be recalled.",
            code="global_sending_paused",
        )

    if account.state == "disabled":
        raise PolicyViolation("Account is disabled", code="account_disabled")
    if account.state != "connected":
        raise PolicyViolation(
            f"Account is not connected (state: {account.state})",
            code=f"account_{account.state}",
        )

    if kind != "diagnostic_test":
        if policy.mode == "paused":
            raise PolicyViolation("Account sending is paused", code="account_paused")
        if policy.mode != "production" or not policy.production_enabled:
            raise PolicyViolation(
                "Account is in test-only mode; transactional sending is disabled",
                code="account_test_only",
            )

    if policy.circuit_state == "open":
        requires_manual = MANUAL_RESET_MARKER in (policy.circuit_reason or "")
        if requires_manual or policy.circuit_open_until is None or ensure_utc(policy.circuit_open_until) > moment:
            raise PolicyViolation(
                f"Sending circuit is open: {policy.circuit_reason or 'unspecified reason'}",
                code="circuit_open",
                details={"open_until": policy.circuit_open_until.isoformat() if policy.circuit_open_until else None},
            )


async def check_test_recipient(session: AsyncSession, email: str) -> AllowedTestRecipient:
    normalized = email.strip().lower()
    recipient = await session.scalar(
        select(AllowedTestRecipient).where(
            func.lower(AllowedTestRecipient.recipient_email) == normalized
        )
    )
    if recipient is None:
        raise PolicyViolation(
            "Test recipients must be on the verified allowlist",
            code="test_recipient_not_allowed",
        )
    if not recipient.is_enabled:
        raise PolicyViolation("Test recipient is disabled", code="test_recipient_disabled")
    if recipient.verified_at is None:
        raise PolicyViolation(
            "Test recipient has not been verified", code="test_recipient_unverified"
        )
    return recipient


@dataclass(frozen=True)
class QuotaSnapshot:
    per_minute_used: int
    per_minute_limit: int
    rolling_24h_used: int
    rolling_24h_limit: int
    in_flight: int
    max_in_flight: int

    def as_dict(self) -> dict[str, int]:
        return {
            "per_minute_used": self.per_minute_used,
            "per_minute_limit": self.per_minute_limit,
            "rolling_24h_used": self.rolling_24h_used,
            "rolling_24h_limit": self.rolling_24h_limit,
            "in_flight": self.in_flight,
            "max_in_flight": self.max_in_flight,
        }


async def quota_snapshot(
    session: AsyncSession,
    account_id: uuid.UUID,
    policy: SendPolicy,
    *,
    now: datetime | None = None,
) -> QuotaSnapshot:
    moment = ensure_utc(now) or utcnow()
    day_ago = moment - timedelta(hours=24)
    minute_ago = moment - timedelta(minutes=1)

    rolling = await session.scalar(
        select(func.count())
        .select_from(QuotaReservation)
        .where(
            QuotaReservation.account_id == account_id,
            QuotaReservation.status == "consumed",
            QuotaReservation.consumed_at > day_ago,
        )
    )
    minute = await session.scalar(
        select(func.count())
        .select_from(QuotaReservation)
        .where(
            QuotaReservation.account_id == account_id,
            QuotaReservation.status == "consumed",
            QuotaReservation.consumed_at > minute_ago,
        )
    )
    inflight = await session.scalar(
        select(func.count())
        .select_from(QuotaReservation)
        .where(
            QuotaReservation.account_id == account_id,
            QuotaReservation.status == "reserved",
        )
    )
    return QuotaSnapshot(
        per_minute_used=int(minute or 0),
        per_minute_limit=policy.max_per_minute,
        rolling_24h_used=int(rolling or 0),
        rolling_24h_limit=policy.max_rolling_24h,
        in_flight=int(inflight or 0),
        max_in_flight=policy.max_in_flight,
    )


async def reserve_quota(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    message_id: uuid.UUID,
    now: datetime | None = None,
) -> QuotaReservation:
    """Reserve one submission slot. Must be called inside a transaction."""

    moment = ensure_utc(now) or utcnow()

    policy = await session.scalar(
        select(SendPolicy).where(SendPolicy.account_id == account_id).with_for_update()
    )
    if policy is None:
        raise PolicyViolation("Send policy is missing for this account", code="policy_missing")

    existing = await session.scalar(
        select(QuotaReservation).where(QuotaReservation.message_id == message_id)
    )
    if existing is not None and existing.status == "reserved":
        return existing

    snapshot = await quota_snapshot(session, account_id, policy, now=moment)

    if snapshot.rolling_24h_used >= policy.max_rolling_24h:
        raise QuotaExceeded(
            "Rolling 24-hour sending cap reached for this account",
            details={
                "window": "24h",
                "used": snapshot.rolling_24h_used,
                "limit": policy.max_rolling_24h,
            },
        )

    if snapshot.per_minute_used >= policy.max_per_minute:
        oldest = await session.scalar(
            select(func.min(QuotaReservation.consumed_at)).where(
                QuotaReservation.account_id == account_id,
                QuotaReservation.status == "consumed",
                QuotaReservation.consumed_at > moment - timedelta(minutes=1),
            )
        )
        retry_after = 60
        if oldest is not None:
            retry_after = max(1, 60 - int((moment - ensure_utc(oldest)).total_seconds()))
        raise QuotaExceeded(
            "Per-minute sending cap reached for this account",
            details={
                "window": "1m",
                "used": snapshot.per_minute_used,
                "limit": policy.max_per_minute,
                "retry_after_seconds": retry_after,
            },
        )

    if snapshot.in_flight >= policy.max_in_flight:
        raise QuotaExceeded(
            "Another submission is already in flight for this account",
            details={
                "window": "in_flight",
                "used": snapshot.in_flight,
                "limit": policy.max_in_flight,
                "retry_after_seconds": 5,
            },
        )

    if existing is not None:
        # An operator requeue starts a new submission for the same message.
        # Reuse the reservation row (schema keeps one per message); the
        # snapshot above already counted the prior consumption when relevant.
        existing.status = "reserved"
        existing.reserved_at = moment
        existing.consumed_at = None
        existing.released_at = None
        existing.release_reason = None
        await session.flush()
        return existing

    reservation = QuotaReservation(
        account_id=account_id,
        message_id=message_id,
        status="reserved",
        reserved_at=moment,
    )
    session.add(reservation)
    await session.flush()
    return reservation


async def consume_quota(
    session: AsyncSession, *, message_id: uuid.UUID, now: datetime | None = None
) -> None:
    reservation = await session.scalar(
        select(QuotaReservation).where(QuotaReservation.message_id == message_id).with_for_update()
    )
    if reservation is None or reservation.status != "reserved":
        return
    reservation.status = "consumed"
    reservation.consumed_at = ensure_utc(now) or utcnow()


async def release_quota(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    reason: str,
    now: datetime | None = None,
) -> None:
    reservation = await session.scalar(
        select(QuotaReservation).where(QuotaReservation.message_id == message_id).with_for_update()
    )
    if reservation is None or reservation.status != "reserved":
        return
    reservation.status = "released"
    reservation.released_at = ensure_utc(now) or utcnow()
    reservation.release_reason = reason[:200]


def circuit_requires_manual_reset(policy: SendPolicy) -> bool:
    return MANUAL_RESET_MARKER in (policy.circuit_reason or "")


async def open_circuit(
    session: AsyncSession,
    *,
    policy: SendPolicy,
    account: EmailAccount,
    reason: str,
    trigger: str,
    requires_manual: bool,
    open_until: datetime | None = None,
    message_id: uuid.UUID | None = None,
) -> None:
    if policy.circuit_state == "open" and policy.circuit_reason == reason:
        return
    suffix = f" ({MANUAL_RESET_MARKER})" if requires_manual else ""
    policy.circuit_state = "open"
    policy.circuit_reason = f"{trigger}: {reason}{suffix}"[:500]
    policy.circuit_open_until = open_until
    policy.updated_at = utcnow()

    await record_safety_event(
        session,
        severity="critical" if requires_manual else "warning",
        event_type="circuit_opened",
        action_taken="sending blocked pending review" if requires_manual else "sending paused temporarily",
        account_id=account.id,
        message_id=message_id,
        details={
            "trigger": trigger,
            "reason": reason,
            "requires_manual_reset": requires_manual,
            "open_until": open_until.isoformat() if open_until else None,
        },
    )


async def close_circuit(
    session: AsyncSession,
    *,
    policy: SendPolicy,
    account: EmailAccount,
    reason: str,
    actor: str,
    request_id: str | None = None,
    source_ip: str | None = None,
) -> None:
    policy.circuit_state = "closed"
    policy.circuit_reason = None
    policy.circuit_open_until = None
    policy.updated_at = utcnow()
    await record_audit(
        session,
        actor=actor,
        action="circuit.reset",
        target_type="email_account",
        target_id=account.id,
        request_id=request_id,
        source_ip=source_ip,
        details={"reason": reason},
    )
    await record_safety_event(
        session,
        severity="info",
        event_type="circuit_closed",
        action_taken="sending permitted again",
        account_id=account.id,
        details={"reason": reason, "actor": actor},
    )


async def assert_sender_allowed(account: EmailAccount, from_email: str) -> None:
    candidate = from_email.strip().lower()
    mailbox = account.mailbox_email.strip().lower()
    if candidate == mailbox:
        return
    metadata = account.provider_metadata or {}
    aliases = metadata.get("allowed_from") or []
    if isinstance(aliases, list) and candidate in {str(a).strip().lower() for a in aliases}:
        return
    raise PolicyViolation(
        "Sender address must match the connected mailbox or a verified alias",
        code="sender_identity_mismatch",
    )


async def message_is_sendable_now(
    session: AsyncSession,
    *,
    message: EmailMessage,
    account: EmailAccount,
    policy: SendPolicy,
    now: datetime | None = None,
) -> None:
    moment = ensure_utc(now) or utcnow()
    if message.expires_at is not None and ensure_utc(message.expires_at) <= moment:
        raise PolicyViolation("Message has expired", code="message_expired")
    await assert_send_allowed(
        session, account=account, policy=policy, kind=message.kind, now=moment
    )


DEFAULT_POLICY = {
    "max_per_minute": settings.default_max_per_minute,
    "max_rolling_24h": settings.default_max_rolling_24h,
    "max_attempts": settings.default_max_attempts,
    "max_in_flight": settings.default_max_in_flight,
}
