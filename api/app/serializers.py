"""Response serializers.

Nothing here reads a secret value. Credentials are reduced to metadata.
"""

from __future__ import annotations

import uuid
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import iso
from app.models import (
    DeliveryAttempt,
    DiagnosticReport,
    DomainAuthCheck,
    EmailAccount,
    EmailMessage,
    ProviderCredential,
    ProviderEvent,
    SendPolicy,
    TestRun,
    TestRunResult,
)
from app.providers.registry import provider_for_transport
from app.services import policy as policy_service
from app.services.credentials import get_credential


def policy_payload(policy: SendPolicy | None) -> dict[str, Any] | None:
    if policy is None:
        return None
    return {
        "mode": policy.mode,
        "production_enabled": policy.production_enabled,
        "max_per_minute": policy.max_per_minute,
        "max_rolling_24h": policy.max_rolling_24h,
        "max_attempts": policy.max_attempts,
        "max_in_flight": policy.max_in_flight,
        "circuit": {
            "state": policy.circuit_state,
            "open_until": iso(policy.circuit_open_until),
            "reason": policy.circuit_reason,
            "requires_manual_reset": policy_service.circuit_requires_manual_reset(policy),
        },
        "require_auth_checks": policy.require_auth_checks,
        "require_test_recipient_allowlist": policy.require_test_recipient_allowlist,
        "updated_at": iso(policy.updated_at),
    }


def credential_payload(credential: ProviderCredential | None) -> dict[str, Any] | None:
    if credential is None:
        return None
    return {
        "configured": True,
        "backend": credential.secret_backend,
        "credential_version": credential.credential_version,
        "granted_scopes": list(credential.granted_scopes or []),
        "access_token_expires_at": iso(credential.access_token_expires_at),
        "last_refresh_at": iso(credential.last_refresh_at),
        "last_validated_at": iso(credential.last_validated_at),
        "updated_at": iso(credential.updated_at),
    }


async def account_payload(
    session: AsyncSession,
    account: EmailAccount,
    *,
    include_readiness: bool = True,
    include_quota: bool = True,
) -> dict[str, Any]:
    policy = await session.get(SendPolicy, account.id)
    credential = await get_credential(session, account.id)
    try:
        capabilities = provider_for_transport(account.transport).capabilities.as_dict()
    except Exception:  # noqa: BLE001
        capabilities = None

    payload: dict[str, Any] = {
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
        "provider_metadata": {
            key: value
            for key, value in (account.provider_metadata or {}).items()
            if key
            in {
                "smtp_host",
                "smtp_port",
                "smtp_security",
                "smtp_username",
                "dkim_selectors",
                "auth_policy",
                "allowed_from",
                "sender_identity_verified",
            }
        },
        "created_at": iso(account.created_at),
        "updated_at": iso(account.updated_at),
        "policy": policy_payload(policy),
        "credentials": credential_payload(credential),
        "capabilities": capabilities,
    }

    if include_readiness and policy is not None:
        report = await policy_service.build_readiness(session, account, policy=policy)
        payload["readiness"] = report.as_dict()

    if include_quota and policy is not None:
        snapshot = await policy_service.quota_snapshot(session, account.id, policy)
        payload["quota"] = snapshot.as_dict()

    payload["auth_checks"] = await policy_service.latest_auth_statuses(session, account.id)
    return payload


def message_summary(message: EmailMessage) -> dict[str, Any]:
    return {
        "id": str(message.id),
        "account_id": str(message.account_id),
        "kind": message.kind,
        "status": message.status,
        "to": message.to_email,
        "from": message.from_email,
        "reply_to": message.reply_to_email,
        "subject": message.subject,
        "scheduled_at": iso(message.scheduled_at),
        "expires_at": iso(message.expires_at),
        "created_at": iso(message.created_at),
        "updated_at": iso(message.updated_at),
        "attempt_count": message.attempt_count,
        "provider_message_id": message.provider_message_id,
        "submitted_at": iso(message.submitted_at),
        "delivery_evidence_at": iso(message.delivery_evidence_at),
        "next_attempt_at": iso(message.next_attempt_at),
        "final_error_code": message.final_error_code,
        "final_error_summary": message.final_error_summary,
        "idempotency_key": message.idempotency_key,
    }


def message_detail(
    message: EmailMessage,
    *,
    attempts: Sequence[DeliveryAttempt] = (),
    events: Sequence[ProviderEvent] = (),
    diagnostics: Sequence[DiagnosticReport] = (),
) -> dict[str, Any]:
    payload = message_summary(message)
    payload.update(
        {
            "text_body": message.text_body,
            "html_body": message.html_body,
            "template_key": message.template_key,
            "template_version": message.template_version,
            "request_fingerprint": message.request_fingerprint,
            "lease_owner": message.lease_owner,
            "lease_expires_at": iso(message.lease_expires_at),
            "attempts": [attempt_payload(row) for row in attempts],
            "events": [provider_event_payload(row) for row in events],
            "diagnostics": [diagnostic_payload(row) for row in diagnostics],
        }
    )
    return payload


def attempt_payload(attempt: DeliveryAttempt) -> dict[str, Any]:
    return {
        "id": str(attempt.id),
        "attempt_number": attempt.attempt_number,
        "provider": attempt.provider,
        "outcome": attempt.outcome,
        "started_at": iso(attempt.started_at),
        "finished_at": iso(attempt.finished_at),
        "duration_ms": attempt.duration_ms,
        "http_status": attempt.http_status,
        "smtp_code": attempt.smtp_code,
        "enhanced_smtp_code": attempt.enhanced_smtp_code,
        "provider_message_id": attempt.provider_message_id,
        "retry_after_seconds": attempt.retry_after_seconds,
        "diagnostic_code": attempt.diagnostic_code,
        "sanitized_detail": attempt.sanitized_detail,
        "response_metadata": attempt.response_metadata,
    }


def provider_event_payload(event: ProviderEvent) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "message_id": str(event.message_id) if event.message_id else None,
        "provider": event.provider,
        "provider_event_id": event.provider_event_id,
        "event_type": event.event_type,
        "event_at": iso(event.event_at),
        "received_at": iso(event.received_at),
        "recipient_email": event.recipient_email,
        "diagnostic_code": event.diagnostic_code,
        "sanitized_detail": event.sanitized_detail,
        "metadata": event.event_metadata,
    }


def diagnostic_payload(report: DiagnosticReport) -> dict[str, Any]:
    return {
        "id": str(report.id),
        "engine": report.engine,
        "summary": report.summary,
        "likely_causes": report.likely_causes,
        "recommended_actions": report.recommended_actions,
        "confidence": report.confidence,
        "needs_operator_action": report.needs_operator_action,
        "created_at": iso(report.created_at),
        "source_attempt_id": str(report.source_attempt_id) if report.source_attempt_id else None,
    }


def domain_check_payload(row: DomainAuthCheck) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "account_id": str(row.account_id) if row.account_id else None,
        "domain": row.domain,
        "check_type": row.check_type,
        "selector": row.selector,
        "status": row.status,
        "observed_value": row.observed_value,
        "evidence": row.evidence,
        "checked_at": iso(row.checked_at),
        "expires_at": iso(row.expires_at),
    }


def test_result_payload(row: TestRunResult) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "allowed_recipient_id": str(row.allowed_recipient_id),
        "message_id": str(row.message_id) if row.message_id else None,
        "status": row.status,
        "observed_at": iso(row.observed_at),
        "observed_inbox": row.observed_inbox,
        "authentication_results": row.authentication_results,
        "latency_ms": row.latency_ms,
        "result_detail": row.result_detail,
    }


def test_run_payload(run: TestRun, results: Sequence[TestRunResult] = ()) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "account_id": str(run.account_id),
        "status": run.status,
        "initiated_by": run.initiated_by,
        "test_profile": run.test_profile,
        "started_at": iso(run.started_at),
        "completed_at": iso(run.completed_at),
        "created_at": iso(run.created_at),
        "summary": run.summary,
        "results": [test_result_payload(row) for row in results],
    }


def recipient_payload(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "recipient_email": row.recipient_email,
        "label": row.label,
        "verification_method": row.verification_method,
        "verified_at": iso(row.verified_at),
        "is_enabled": row.is_enabled,
        "created_at": iso(row.created_at),
    }


def suppression_payload(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "recipient_email": row.recipient_email,
        "reason": row.reason,
        "source_account_id": str(row.source_account_id) if row.source_account_id else None,
        "source_message_id": str(row.source_message_id) if row.source_message_id else None,
        "evidence_event_id": str(row.evidence_event_id) if row.evidence_event_id else None,
        "created_at": iso(row.created_at),
        "removed_at": iso(row.removed_at),
        "removed_by": row.removed_by,
        "removal_reason": row.removal_reason,
    }


def new_id() -> uuid.UUID:
    return uuid.uuid4()
