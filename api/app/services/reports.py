"""Operator reporting: delivery truth, quota usage, and recent risk signals."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import iso, utcnow
from app.models import (
    AuditLog,
    DeliveryAttempt,
    EmailAccount,
    EmailMessage,
    ProviderEvent,
    SafetyEvent,
    SendPolicy,
)
from app.services import policy as policy_service
from app.services import settings as app_settings
from app.services.queue import backlog_snapshot

STATUSES = (
    "queued",
    "sending",
    "retry_wait",
    "submitted",
    "delivered",
    "bounced",
    "complained",
    "failed",
    "needs_review",
    "cancelled",
    "expired",
)


async def message_counts(
    session: AsyncSession, *, days: int, account_id: uuid.UUID | None = None
) -> dict[str, int]:
    cutoff = utcnow() - timedelta(days=days)
    statement = (
        select(EmailMessage.status, func.count())
        .where(EmailMessage.created_at >= cutoff)
        .group_by(EmailMessage.status)
    )
    if account_id is not None:
        statement = statement.where(EmailMessage.account_id == account_id)
    rows = (await session.execute(statement)).all()
    counts = {status: 0 for status in STATUSES}
    for status, count in rows:
        counts[str(status)] = int(count)
    return counts


async def attempt_outcomes(
    session: AsyncSession, *, days: int, account_id: uuid.UUID | None = None
) -> dict[str, int]:
    cutoff = utcnow() - timedelta(days=days)
    statement = (
        select(DeliveryAttempt.outcome, func.count())
        .join(EmailMessage, EmailMessage.id == DeliveryAttempt.message_id)
        .where(DeliveryAttempt.started_at >= cutoff)
        .group_by(DeliveryAttempt.outcome)
    )
    if account_id is not None:
        statement = statement.where(EmailMessage.account_id == account_id)
    rows = (await session.execute(statement)).all()
    return {str(outcome): int(count) for outcome, count in rows}


async def event_counts(
    session: AsyncSession, *, days: int, account_id: uuid.UUID | None = None
) -> dict[str, int]:
    cutoff = utcnow() - timedelta(days=days)
    statement = (
        select(ProviderEvent.event_type, func.count())
        .where(ProviderEvent.received_at >= cutoff)
        .group_by(ProviderEvent.event_type)
    )
    if account_id is not None:
        statement = statement.where(ProviderEvent.account_id == account_id)
    rows = (await session.execute(statement)).all()
    return {str(kind): int(count) for kind, count in rows}


async def account_summaries(session: AsyncSession, *, days: int) -> list[dict[str, Any]]:
    accounts = list(await session.scalars(select(EmailAccount).order_by(EmailAccount.created_at)))
    cutoff = utcnow() - timedelta(days=days)
    summaries: list[dict[str, Any]] = []

    for account in accounts:
        policy = await session.get(SendPolicy, account.id)
        counts = await message_counts(session, days=days, account_id=account.id)
        recent_counts = (
            await session.execute(
                select(EmailMessage.status, func.count())
                .where(
                    EmailMessage.account_id == account.id,
                    EmailMessage.created_at >= cutoff,
                )
                .group_by(EmailMessage.status)
            )
        ).all()
        window = {str(status): int(count) for status, count in recent_counts}
        quota = (
            await policy_service.quota_snapshot(session, account.id, policy)
            if policy is not None
            else None
        )
        readiness = (
            await policy_service.build_readiness(session, account, policy=policy)
            if policy is not None
            else None
        )
        latest_auth = await policy_service.latest_auth_statuses(session, account.id)

        summaries.append(
            {
                "id": str(account.id),
                "provider": account.provider,
                "transport": account.transport,
                "mailbox_email": account.mailbox_email,
                "display_name": account.display_name,
                "sending_domain": account.sending_domain,
                "state": account.state,
                "connection_tested_at": iso(account.connection_tested_at),
                "last_error_code": account.last_error_code,
                "last_error_summary": account.last_error_summary,
                "mode": policy.mode if policy else None,
                "production_enabled": policy.production_enabled if policy else False,
                "circuit": {
                    "state": policy.circuit_state if policy else None,
                    "open_until": iso(policy.circuit_open_until) if policy else None,
                    "reason": policy.circuit_reason if policy else None,
                },
                "quota": quota.as_dict() if quota else None,
                "readiness": readiness.as_dict() if readiness else None,
                "auth_checks": latest_auth,
                "window_counts": window,
                "lifetime_counts": counts,
            }
        )
    return summaries


async def summary(session: AsyncSession, *, days: int = 7) -> dict[str, Any]:
    global_enabled = await app_settings.is_global_sending_enabled(session)
    backlog = await backlog_snapshot(session)
    counts = await message_counts(session, days=days)
    outcomes = await attempt_outcomes(session, days=days)
    events = await event_counts(session, days=days)

    safety_rows = list(
        await session.scalars(
            select(SafetyEvent)
            .order_by(SafetyEvent.occurred_at.desc())
            .limit(15)
        )
    )
    open_circuits = list(
        await session.scalars(
            select(SendPolicy).where(SendPolicy.circuit_state != "closed")
        )
    )

    accepted = outcomes.get("accepted", 0)
    return {
        "generated_at": iso(utcnow()),
        "period_days": days,
        "global_sending_enabled": global_enabled,
        "backlog": backlog,
        "message_counts": counts,
        "attempt_outcomes": outcomes,
        "provider_events": events,
        "delivery_truth": {
            "provider_accepted": accepted,
            "delivery_evidence": counts.get("delivered", 0),
            "hard_bounce_or_complaint": counts.get("bounced", 0) + counts.get("complained", 0),
            "awaiting_evidence": max(0, accepted - counts.get("delivered", 0) - counts.get("bounced", 0)),
            "note": (
                "Provider acceptance is not proof of inbox placement. 'delivered' requires a "
                "provider event or a controlled mailbox observation."
            ),
        },
        "open_circuits": [
            {
                "account_id": str(row.account_id),
                "state": row.circuit_state,
                "reason": row.circuit_reason,
                "open_until": iso(row.circuit_open_until),
            }
            for row in open_circuits
        ],
        "recent_safety_events": [
            {
                "id": str(row.id),
                "severity": row.severity,
                "event_type": row.event_type,
                "action_taken": row.action_taken,
                "occurred_at": iso(row.occurred_at),
                "account_id": str(row.account_id) if row.account_id else None,
                "details": row.details,
            }
            for row in safety_rows
        ],
        "accounts": await account_summaries(session, days=days),
    }


async def audit_feed(session: AsyncSession, *, limit: int = 100) -> list[dict[str, Any]]:
    rows = list(
        await session.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit))
    )
    return [
        {
            "id": str(row.id),
            "actor": row.actor,
            "action": row.action,
            "target_type": row.target_type,
            "target_id": str(row.target_id) if row.target_id else None,
            "result": row.result,
            "request_id": row.request_id,
            "details": row.details,
            "created_at": iso(row.created_at),
        }
        for row in rows
    ]


async def safety_feed(session: AsyncSession, *, limit: int = 100) -> list[dict[str, Any]]:
    rows = list(
        await session.scalars(
            select(SafetyEvent).order_by(SafetyEvent.occurred_at.desc()).limit(limit)
        )
    )
    return [
        {
            "id": str(row.id),
            "severity": row.severity,
            "event_type": row.event_type,
            "action_taken": row.action_taken,
            "account_id": str(row.account_id) if row.account_id else None,
            "message_id": str(row.message_id) if row.message_id else None,
            "details": row.details,
            "occurred_at": iso(row.occurred_at),
            "acknowledged_at": iso(row.acknowledged_at),
        }
        for row in rows
    ]
