"""Advisory delivery diagnostics.

Deterministic rules always run first. The optional LLM receives sanitized
codes and metrics only; it can never change account state, open a circuit, or
schedule a message.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import NotFound
from app.core.redaction import mask_email, sanitize_text
from app.models import DeliveryAttempt, DiagnosticReport, EmailAccount, EmailMessage, ProviderEvent
from app.services.audit import record_audit
from app.services.policy import latest_auth_statuses

logger = logging.getLogger(__name__)

CONFIDENCE = {"low", "medium", "high"}


@dataclass
class Diagnosis:
    summary: str
    likely_causes: list[str] = field(default_factory=list)
    recommended_actions: list[str] = field(default_factory=list)
    confidence: str = "medium"
    needs_operator_action: bool = True


def _haystack(context: dict[str, Any]) -> str:
    parts = [
        str(context.get("diagnostic_code") or ""),
        str(context.get("detail") or ""),
        str(context.get("enhanced_smtp_code") or ""),
        str(context.get("provider_error_code") or ""),
        f"smtp={context.get('smtp_code')}",
        f"http={context.get('http_status')}",
    ]
    return " ".join(parts).lower()


def _recipient_problem(text: str, context: dict[str, Any]) -> bool:
    return any(
        marker in text
        for marker in (
            "5.1.1",
            "user unknown",
            "recipient not found",
            "does not exist",
            "invalid recipient",
            "mailbox unavailable",
            "550 5.1",
        )
    )


def rule_diagnosis(context: dict[str, Any]) -> Diagnosis:
    text = _haystack(context)
    auth = context.get("auth_checks") or {}
    retryability = context.get("retryability")

    if "auth" in text or context.get("account_state") == "auth_required" or retryability == "auth":
        return Diagnosis(
            summary="The provider rejected the credentials or the granted permissions.",
            likely_causes=[
                "Refresh token revoked, expired, or password rotated",
                "OAuth consent removed or scopes reduced",
                "Tenant policy blocking the client application",
            ],
            recommended_actions=[
                "Reconnect the account and complete the consent flow again",
                "Verify the application's OAuth scopes and redirect URI",
                "Check the provider tenant's conditional-access or SMTP AUTH policy",
            ],
            confidence="high",
        )

    if "tls" in text or "certificate" in text or "starttls" in text:
        return Diagnosis(
            summary="The SMTP endpoint did not complete a verified TLS handshake.",
            likely_causes=[
                "Expired or mismatched certificate",
                "Server does not offer STARTTLS on the configured port",
                "Intermediate proxy intercepting the connection",
            ],
            recommended_actions=[
                "Confirm the host, port, and encryption mode with the provider",
                "Renew or correct the certificate; do not disable verification",
                "Use port 465 (implicit TLS) or 587 (STARTTLS) as the provider documents",
            ],
            confidence="high",
        )

    if retryability == "rate_limited" or any(
        marker in text for marker in ("rate limit", "quota", "too many", "throttl", "429", "4.7.")
    ):
        return Diagnosis(
            summary="The provider is throttling or enforcing a sending limit.",
            likely_causes=[
                "Internal per-minute or rolling cap reached",
                "Provider-side rate limit or temporary quota restriction",
                "Too many concurrent submissions for the mailbox",
            ],
            recommended_actions=[
                "Wait for the retry window and keep the configured caps conservative",
                "Confirm the account's actual provider quota before raising limits",
                "Do not rotate accounts to evade a provider restriction",
            ],
            confidence="high",
        )

    if _recipient_problem(text, context):
        return Diagnosis(
            summary="The recipient address was rejected as invalid or unknown.",
            likely_causes=[
                "Mailbox does not exist or was disabled",
                "Typo in the recipient address",
                "Recipient server rejects the address for policy reasons",
            ],
            recommended_actions=[
                "Confirm the address with the recipient through another channel",
                "Keep the address suppressed; do not retry it",
                "Fix the address in the source system before sending again",
            ],
            confidence="high",
        )

    if "5.2.2" in text or "mailbox full" in text or "over quota" in text:
        return Diagnosis(
            summary="The recipient mailbox is over quota.",
            likely_causes=["Recipient mailbox storage is full", "Recipient account is inactive"],
            recommended_actions=[
                "Treat as a temporary failure and retry later within the message expiry",
                "Contact the recipient through another channel",
            ],
            confidence="medium",
            needs_operator_action=False,
        )

    if any(marker in text for marker in ("5.7.1", "spam", "policy", "blocked", "reputation", "dmarc")):
        return Diagnosis(
            summary="The receiving side rejected the message on policy, authentication, or reputation grounds.",
            likely_causes=[
                "SPF, DKIM, or DMARC not aligned for the visible From domain",
                "Content or links triggered a policy filter",
                "Sending domain or IP has limited reputation",
            ],
            recommended_actions=[
                "Run domain checks and fix authentication before sending more",
                "Review the message content for links or claims that commonly trigger filters",
                "Reduce volume and send only expected transactional mail",
            ],
            confidence="medium",
        )

    missing = [name for name, status in auth.items() if status in {"fail", "unknown", "warning"}]
    if missing:
        return Diagnosis(
            summary="Domain authentication is incomplete, which limits deliverability.",
            likely_causes=[f"{name.upper()} is not passing" for name in sorted(missing)],
            recommended_actions=[
                "Publish or correct the missing DNS records",
                "Re-run domain checks after DNS propagation",
                "Keep DMARC at p=none until SPF and DKIM align, then move to quarantine/reject",
            ],
            confidence="medium",
        )

    if retryability == "unknown" or "ambiguous" in text or "timeout" in text:
        return Diagnosis(
            summary="The submission outcome is ambiguous: the provider may have accepted the message.",
            likely_causes=[
                "Network timeout or connection reset after the request was sent",
                "Worker crash between submission and outcome recording",
            ],
            recommended_actions=[
                "Check the provider's Sent folder or message log before resubmitting",
                "Only requeue after confirming the message was not accepted",
                "Give time-sensitive messages a short expiry so stale retries fail closed",
            ],
            confidence="high",
        )

    return Diagnosis(
        summary="No specific failure pattern was recognized.",
        likely_causes=[
            "Transient provider or network error",
            "Insufficient evidence in the recorded attempt",
        ],
        recommended_actions=[
            "Inspect the delivery attempts and provider response metadata",
            "Retry within the configured limits if the error is transient",
            "If it repeats, capture the provider error code for support",
        ],
        confidence="low",
    )


_SYSTEM_PROMPT = """You are an email deliverability diagnostic assistant for a private operator console.
You explain recorded provider failures. You never authorize sending, never suggest bypassing provider
policies or rate limits, and never suggest manufacturing engagement. Return compact JSON with keys:
summary (string), likely_causes (array of short strings), recommended_actions (array of short strings),
confidence ("low"|"medium"|"high"), needs_operator_action (boolean)."""


async def _llm_diagnosis(context: dict[str, Any]) -> Diagnosis:
    if not settings.llm_enabled or not settings.llm_api_key:
        raise RuntimeError("LLM diagnostics are not enabled")

    prompt_payload = json.dumps(context, sort_keys=True)[: settings.llm_max_input_chars]
    payload = {
        "model": settings.llm_model,
        "temperature": 0.2,
        "max_tokens": 600,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt_payload},
        ],
    }
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
        response = await client.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions", headers=headers, json=payload
        )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    parsed = json.loads(content)

    summary = sanitize_text(str(parsed.get("summary", "")), limit=400)
    if not summary:
        raise ValueError("LLM response had no summary")

    def _clean_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned = [sanitize_text(str(item), limit=200) for item in value[:5]]
        return [item for item in cleaned if item]

    confidence = str(parsed.get("confidence", "medium")).lower()
    return Diagnosis(
        summary=summary,
        likely_causes=_clean_list(parsed.get("likely_causes")),
        recommended_actions=_clean_list(parsed.get("recommended_actions")),
        confidence=confidence if confidence in CONFIDENCE else "medium",
        needs_operator_action=bool(parsed.get("needs_operator_action", True)),
    )


def sanitized_context(
    *,
    account: EmailAccount | None,
    message: EmailMessage | None,
    attempt: DeliveryAttempt | None,
    auth_checks: dict[str, str],
    consecutive_events: dict[str, int],
    max_attempts: int | None,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "provider": account.provider if account else None,
        "transport": account.transport if account else None,
        "account_state": account.state if account else None,
        "auth_checks": auth_checks,
        "event_counts": consecutive_events,
        "message_status": message.status if message else None,
        "kind": message.kind if message else None,
        "attempt_count": message.attempt_count if message else None,
        "max_attempts": max_attempts,
        "retryability": None,
        "diagnostic_code": None,
        "detail": None,
        "http_status": None,
        "smtp_code": None,
        "enhanced_smtp_code": None,
        "provider_error_code": None,
    }
    if message is not None:
        context["recipient_domain"] = (
            message.to_email.rsplit("@", 1)[1].lower() if "@" in message.to_email else None
        )
        context["recipient"] = mask_email(message.to_email)
    if attempt is not None:
        context.update(
            {
                "retryability": {
                    "accepted": "accepted",
                    "transient_failure": "transient",
                    "permanent_failure": "permanent",
                    "auth_failure": "auth",
                    "rate_limited": "rate_limited",
                    "unknown_outcome": "unknown",
                }.get(attempt.outcome),
                "diagnostic_code": attempt.diagnostic_code,
                "detail": sanitize_text(attempt.sanitized_detail, limit=300),
                "http_status": attempt.http_status,
                "smtp_code": attempt.smtp_code,
                "enhanced_smtp_code": attempt.enhanced_smtp_code,
                "provider_error_code": (attempt.response_metadata or {}).get("provider_error_code"),
                "attempts_recorded": attempt.attempt_number,
            }
        )
    return context


async def diagnose_message(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    account_id: uuid.UUID,
    use_llm: bool = True,
    actor: str = "operator",
) -> DiagnosticReport:
    message = await session.get(EmailMessage, message_id)
    if message is None:
        raise NotFound("Message not found")

    account = await session.get(EmailAccount, account_id)
    attempt = await session.scalar(
        select(DeliveryAttempt)
        .where(DeliveryAttempt.message_id == message_id)
        .order_by(DeliveryAttempt.attempt_number.desc())
        .limit(1)
    )
    event_counts_rows = (
        await session.execute(
            select(ProviderEvent.event_type, func.count())
            .where(ProviderEvent.message_id == message_id)
            .group_by(ProviderEvent.event_type)
        )
    ).all()
    event_counts = {row[0]: int(row[1]) for row in event_counts_rows}
    auth_checks = await latest_auth_statuses(session, account_id) if account else {}

    from app.models import SendPolicy

    policy = await session.get(SendPolicy, account_id)
    context = sanitized_context(
        account=account,
        message=message,
        attempt=attempt,
        auth_checks=auth_checks,
        consecutive_events=event_counts,
        max_attempts=policy.max_attempts if policy else None,
    )

    diagnosis = rule_diagnosis(context)
    engine = "rules"

    if use_llm and settings.llm_enabled:
        try:
            diagnosis = await _llm_diagnosis(context)
            engine = "llm"
        except Exception:  # noqa: BLE001 - AI is advisory; never fail the request
            logger.warning("LLM diagnosis failed; falling back to rules", exc_info=True)

    report = DiagnosticReport(
        account_id=account_id,
        message_id=message_id,
        source_attempt_id=attempt.id if attempt else None,
        engine=engine,
        summary=diagnosis.summary,
        likely_causes=diagnosis.likely_causes,
        recommended_actions=diagnosis.recommended_actions,
        confidence=diagnosis.confidence if diagnosis.confidence in CONFIDENCE else "medium",
        needs_operator_action=diagnosis.needs_operator_action,
    )
    session.add(report)
    await session.flush()

    await record_audit(
        session,
        actor=actor,
        action="diagnostics.generate",
        target_type="email_message",
        target_id=message_id,
        details={"engine": engine, "report_id": str(report.id)},
    )
    return report
