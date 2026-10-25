"""Controlled deliverability tests.

Tests only go to verified, allowlisted recipients, carry an unmistakable test
header, and never claim inbox placement without an actual observation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound, PolicyViolation
from app.core.headers import header_slice, parse_authentication_results
from app.core.time import ensure_utc, utcnow
from app.models import (
    AllowedTestRecipient,
    DomainAuthCheck,
    EmailAccount,
    EmailMessage,
    SendPolicy,
    TestRun,
    TestRunResult,
)
from app.services.audit import record_audit, record_safety_event
from app.services.domain_checks import account_domain

TEST_PROFILES = ("basic_delivery", "authentication", "latency")
TERMINAL_RESULT_STATUSES = frozenset(
    {"observed_received", "auth_pass", "warning", "failed", "timed_out"}
)


def _subject(profile: str, run_id: uuid.UUID, domain: str) -> str:
    return f"[DELIVERABILITY TEST {str(run_id)[:8]}] {profile} from {domain}"


def _body(profile: str, run_id: uuid.UUID, domain: str) -> str:
    return (
        "This is a controlled deliverability test sent by the private operator console.\n\n"
        f"Run: {run_id}\n"
        f"Profile: {profile}\n"
        f"Sending domain: {domain}\n"
        f"Generated: {utcnow().isoformat()}\n\n"
        "It was sent only to an address the operator explicitly authorized. "
        "Compare SPF, DKIM, and DMARC results in the received headers; provider "
        "acceptance alone is not proof of inbox placement.\n"
    )


async def create_run(
    session: AsyncSession,
    *,
    account: EmailAccount,
    recipients: Sequence[AllowedTestRecipient],
    profile: str,
    actor: str,
    request_id: str | None = None,
    source_ip: str | None = None,
) -> TestRun:
    if profile not in TEST_PROFILES:
        raise PolicyViolation(f"Unknown test profile: {profile}", code="invalid_test_profile")
    if not recipients:
        raise PolicyViolation("At least one verified test recipient is required", code="no_test_recipients")
    if account.state != "connected":
        raise PolicyViolation(
            "Connect and validate the account before running tests",
            code="account_not_connected",
        )

    policy = await session.get(SendPolicy, account.id)
    if policy is not None and policy.mode == "paused":
        raise PolicyViolation("Account is paused", code="account_paused")

    domain = account_domain(account)
    run = TestRun(
        account_id=account.id,
        status="queued",
        initiated_by=actor,
        test_profile=profile,
        summary={"requested_recipients": len(recipients)},
    )
    session.add(run)
    await session.flush()

    now = utcnow()
    for recipient in recipients:
        if not recipient.is_enabled or recipient.verified_at is None:
            raise PolicyViolation(
                "Every test recipient must be verified and enabled",
                code="test_recipient_unverified",
            )
        message = EmailMessage(
            account_id=account.id,
            kind="diagnostic_test",
            status="queued",
            to_email=recipient.recipient_email,
            from_email=account.mailbox_email,
            subject=_subject(profile, run.id, domain),
            text_body=_body(profile, run.id, domain),
            scheduled_at=now,
            expires_at=now + timedelta(hours=6),
        )
        session.add(message)
        await session.flush()
        session.add(
            TestRunResult(
                test_run_id=run.id,
                allowed_recipient_id=recipient.id,
                message_id=message.id,
                status="pending",
            )
        )

    run.summary = {
        "requested_recipients": len(recipients),
        "profile": profile,
        "domain": domain,
    }
    await session.flush()

    await record_audit(
        session,
        actor=actor,
        action="test.run.create",
        target_type="test_run",
        target_id=run.id,
        request_id=request_id,
        source_ip=source_ip,
        details={
            "account_id": str(account.id),
            "recipients": [r.recipient_email for r in recipients],
            "profile": profile,
        },
    )
    return run


async def results_for_run(session: AsyncSession, run_id: uuid.UUID) -> list[TestRunResult]:
    return list(
        await session.scalars(
            select(TestRunResult)
            .where(TestRunResult.test_run_id == run_id)
            .order_by(TestRunResult.id)
        )
    )


async def refresh_run_status(session: AsyncSession, run_id: uuid.UUID) -> TestRun:
    run = await session.get(TestRun, run_id)
    if run is None:
        raise NotFound("Test run not found")
    results = await results_for_run(session, run_id)
    statuses = [r.status for r in results]
    now = utcnow()

    if not statuses:
        run.status = "failed"
        run.completed_at = now
        return run

    if any(status == "pending" for status in statuses):
        started = any(
            status in {"submitted", "observed_received", "auth_pass", "warning"} for status in statuses
        )
        if run.started_at is None and started:
            run.started_at = now
        run.status = "running" if started else "queued"
        return run

    run.completed_at = now
    if run.started_at is None:
        run.started_at = now
    if any(status in {"failed", "timed_out", "warning"} for status in statuses):
        run.status = "completed_with_errors"
    else:
        run.status = "completed"
    run.summary = {
        **(run.summary or {}),
        "counts": {status: statuses.count(status) for status in sorted(set(statuses))},
    }
    return run


async def on_message_state_change(session: AsyncSession, message: EmailMessage) -> None:
    """Hook called from finalize for diagnostic-test messages."""

    if message.kind != "diagnostic_test":
        return

    result = await session.scalar(
        select(TestRunResult).where(TestRunResult.message_id == message.id)
    )
    if result is None:
        return

    if message.status == "submitted":
        if result.status == "pending":
            result.status = "submitted"
        run = await session.get(TestRun, result.test_run_id)
        if run is not None and run.started_at is None:
            run.started_at = utcnow()
            run.status = "running"
    elif message.status in {"failed", "cancelled", "expired"}:
        result.status = "timed_out" if message.status == "expired" else "failed"
        result.result_detail = (
            message.final_error_summary
            or f"message ended as {message.status}"
        )[:1000]
        await refresh_run_status(session, result.test_run_id)


async def record_observation(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    result_id: uuid.UUID,
    raw_headers: str,
    observed_inbox: str | None,
    observed_at: datetime | None,
    actor: str,
    request_id: str | None = None,
    source_ip: str | None = None,
) -> TestRunResult:
    result = await session.get(TestRunResult, result_id)
    if result is None or result.test_run_id != run_id:
        raise NotFound("Test result not found")
    run = await session.get(TestRun, run_id)
    if run is None:
        raise NotFound("Test run not found")

    moment = ensure_utc(observed_at) or utcnow()
    auth_header = header_slice(raw_headers)
    summary = parse_authentication_results(auth_header) if auth_header else None

    result.observed_at = moment
    result.observed_inbox = observed_inbox

    message = await session.get(EmailMessage, result.message_id) if result.message_id else None
    if message is not None and message.submitted_at is not None:
        delta = moment - ensure_utc(message.submitted_at)
        result.latency_ms = max(0, int(delta.total_seconds() * 1000))

    if summary is None:
        result.status = "observed_received"
        result.authentication_results = {
            "note": "No Authentication-Results header supplied; receipt confirmed only."
        }
        result.result_detail = "Message observed in the controlled mailbox."
    else:
        result.authentication_results = {
            **summary.as_dict(),
            "raw": summary.raw[:4000],
        }
        verdicts = {
            name: mech.result for name, mech in summary.mechanisms.items() if name in {"spf", "dkim", "dmarc"}
        }
        aligned = summary.alignment
        failures = [name for name, verdict in verdicts.items() if verdict not in {"pass"}]
        alignment_failures = [name for name, verdict in aligned.items() if verdict != "pass"]
        if verdicts and not failures and not alignment_failures:
            result.status = "auth_pass"
            result.result_detail = "Observed in the test inbox with passing authentication and alignment."
        else:
            result.status = "warning"
            result.result_detail = (
                "Observed in the test inbox, but authentication or alignment needs attention: "
                + ", ".join(sorted(set(failures + alignment_failures)))
            )[:1000]

        account = await session.get(EmailAccount, run.account_id)
        if account is not None:
            domain = summary.from_domain or account_domain(account)
            status = "pass" if result.status == "auth_pass" else "warning"
            session.add(
                DomainAuthCheck(
                    account_id=account.id,
                    domain=domain,
                    check_type="sender_alignment",
                    status=status,
                    observed_value=summary.raw[:4000],
                    evidence=summary.as_dict(),
                    checked_at=moment,
                )
            )

    await record_audit(
        session,
        actor=actor,
        action="test.result.observe",
        target_type="test_run",
        target_id=run_id,
        request_id=request_id,
        source_ip=source_ip,
        details={
            "result_id": str(result.id),
            "status": result.status,
            "observed_inbox": observed_inbox,
        },
    )
    await refresh_run_status(session, run_id)
    return result


async def cancel_run(
    session: AsyncSession, *, run_id: uuid.UUID, actor: str, reason: str
) -> TestRun:
    run = await session.get(TestRun, run_id)
    if run is None:
        raise NotFound("Test run not found")
    if run.status in {"completed", "completed_with_errors", "cancelled"}:
        return run

    run.status = "cancelled"
    run.completed_at = utcnow()
    results = await results_for_run(session, run_id)
    for result in results:
        if result.status == "pending":
            result.status = "timed_out"
            result.result_detail = "Test run cancelled by the operator."
        if result.message_id:
            message = await session.get(EmailMessage, result.message_id)
            if message is not None and message.status in {"queued", "retry_wait"}:
                message.status = "cancelled"
                message.final_error_code = "test_run_cancelled"
                message.updated_at = utcnow()

    await record_audit(
        session,
        actor=actor,
        action="test.run.cancel",
        target_type="test_run",
        target_id=run_id,
        details={"reason": reason},
    )
    return run


async def timeout_stale_runs(session: AsyncSession, *, older_than_hours: int = 24) -> int:
    cutoff = utcnow() - timedelta(hours=older_than_hours)
    runs = list(
        await session.scalars(
            select(TestRun).where(
                TestRun.status.in_(("queued", "running")), TestRun.created_at < cutoff
            )
        )
    )
    count = 0
    for run in runs:
        results = await results_for_run(session, run.id)
        for result in results:
            if result.status == "pending":
                result.status = "timed_out"
                result.result_detail = "No observation was recorded before the timeout."
        run.status = "completed_with_errors" if results else "failed"
        run.completed_at = utcnow()
        await record_safety_event(
            session,
            severity="warning",
            event_type="test_run_timed_out",
            action_taken="test run closed without a complete observation",
            account_id=run.account_id,
            details={"run_id": str(run.id)},
        )
        count += 1
    return count


async def run_counts(session: AsyncSession, account_id: uuid.UUID | None = None) -> dict[str, int]:
    statement = select(TestRun.status, func.count()).group_by(TestRun.status)
    if account_id is not None:
        statement = statement.where(TestRun.account_id == account_id)
    rows = (await session.execute(statement)).all()
    return {row[0]: int(row[1]) for row in rows}
