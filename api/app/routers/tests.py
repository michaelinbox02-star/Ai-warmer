"""Controlled deliverability tests and the verified recipient allowlist.

Every destination must be an enabled, verified entry on the allowlist. A test
run creates ordinary queued messages with ``kind='diagnostic_test'``; the
sender worker applies the same quota, circuit, and kill-switch checks as for
transactional mail. Provider acceptance is recorded as ``submitted`` and is
never presented as inbox placement. Inbox placement exists only after the
operator records an observation with the actual received headers.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable, Sequence

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, PolicyViolation
from app.core.time import utcnow
from app.db import get_session
from app.models import (
    AllowedTestRecipient,
    EmailAccount,
    SendPolicy,
    TestRun,
    TestRunResult,
)
from app.schemas import (
    CancelRequest,
    ObservationRequest,
    TestRecipientCreateRequest,
    TestRecipientStateRequest,
    TestRunCreateRequest,
)
from app.security import Principal, client_key, require_admin
from app.serializers import recipient_payload, test_run_payload
from app.services import test_runs as test_service
from app.services.audit import record_audit, record_safety_event

router = APIRouter(prefix="/tests", tags=["controlled-tests"])

_SELF_VERIFIED_METHODS = frozenset({"operator_confirmed", "provider_owned_mailbox"})


def _context(request: Request, principal: Principal) -> dict[str, Any]:
    return {
        "request_id": principal.request_id,
        "source_ip": client_key(request),
    }


# ---------------------------------------------------------------------------
# Verified test-recipient allowlist
# ---------------------------------------------------------------------------


async def _get_recipient(
    session: AsyncSession, recipient_id: uuid.UUID
) -> AllowedTestRecipient:
    recipient = await session.get(AllowedTestRecipient, recipient_id)
    if recipient is None:
        raise NotFound("Test recipient not found")
    return recipient


async def _recipient_by_email(
    session: AsyncSession, email: str
) -> AllowedTestRecipient | None:
    return await session.scalar(
        select(AllowedTestRecipient).where(
            func.lower(AllowedTestRecipient.recipient_email) == email.strip().lower()
        )
    )


@router.get("/recipients")
async def list_recipients(
    include_disabled: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    statement = select(AllowedTestRecipient).order_by(
        AllowedTestRecipient.created_at.desc(), AllowedTestRecipient.id.desc()
    )
    if not include_disabled:
        statement = statement.where(AllowedTestRecipient.is_enabled.is_(True))
    rows = list(await session.scalars(statement))
    return {
        "recipients": [recipient_payload(row) for row in rows],
        "note": (
            "Only enabled, verified entries can receive diagnostic tests. "
            "Removing an entry never removes historical test evidence."
        ),
    }


@router.post("/recipients", status_code=201)
async def create_recipient(
    payload: TestRecipientCreateRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    if payload.verification_method not in _SELF_VERIFIED_METHODS:
        raise PolicyViolation(
            "Challenge-link verification is not implemented. Use operator confirmation "
            "for a mailbox you control, or provider_owned_mailbox for the connected mailbox.",
            code="verification_method_not_supported",
        )

    email = str(payload.recipient_email).strip().lower()
    if await _recipient_by_email(session, email) is not None:
        raise Conflict(
            "This recipient is already on the test allowlist",
            code="test_recipient_exists",
        )

    recipient = AllowedTestRecipient(
        recipient_email=email,
        label=payload.label,
        verification_method=payload.verification_method,
        verified_at=utcnow(),
        is_enabled=True,
    )
    session.add(recipient)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(
            "This recipient is already on the test allowlist",
            code="test_recipient_exists",
        ) from exc

    await record_audit(
        session,
        actor=principal.actor,
        action="test.recipient.create",
        target_type="allowed_test_recipient",
        target_id=recipient.id,
        details={
            "recipient_email": email,
            "verification_method": recipient.verification_method,
            "operator_attested_authorization": True,
        },
        **_context(request, principal),
    )
    await session.commit()
    return {
        "recipient": recipient_payload(recipient),
        "note": (
            "The operator confirmed this mailbox is controlled by them or explicitly "
            "authorized for diagnostic mail."
        ),
    }


@router.patch("/recipients/{recipient_id}")
async def update_recipient_state(
    recipient_id: uuid.UUID,
    payload: TestRecipientStateRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    recipient = await _get_recipient(session, recipient_id)
    previous = recipient.is_enabled
    recipient.is_enabled = payload.enabled

    await record_audit(
        session,
        actor=principal.actor,
        action="test.recipient.update_state",
        target_type="allowed_test_recipient",
        target_id=recipient.id,
        details={
            "from": previous,
            "to": payload.enabled,
            "reason": payload.reason,
        },
        **_context(request, principal),
    )
    if previous and not payload.enabled:
        await record_safety_event(
            session,
            severity="info",
            event_type="test_recipient_disabled",
            action_taken="recipient cannot be used for future tests",
            details={"recipient_id": str(recipient.id), "reason": payload.reason},
        )
    await session.commit()
    return {"recipient": recipient_payload(recipient)}


@router.delete("/recipients/{recipient_id}")
async def delete_recipient(
    recipient_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    recipient = await _get_recipient(session, recipient_id)
    used = await session.scalar(
        select(func.count())
        .select_from(TestRunResult)
        .where(TestRunResult.allowed_recipient_id == recipient.id)
    )
    if used:
        raise Conflict(
            "This recipient has test history and cannot be deleted. Disable it instead.",
            code="test_recipient_has_history",
            details={"results": int(used or 0)},
        )

    email = recipient.recipient_email
    await session.delete(recipient)
    await record_audit(
        session,
        actor=principal.actor,
        action="test.recipient.delete",
        target_type="allowed_test_recipient",
        target_id=recipient_id,
        details={"recipient_email": email},
        **_context(request, principal),
    )
    await session.commit()
    return {"deleted": True, "id": str(recipient_id)}


# ---------------------------------------------------------------------------
# Test runs
# ---------------------------------------------------------------------------


async def _resolve_recipients(
    session: AsyncSession,
    *,
    recipient_ids: Sequence[uuid.UUID],
    recipient_emails: Sequence[str],
) -> list[AllowedTestRecipient]:
    """Return allowlisted recipients in request order, without duplicates."""

    by_id: dict[uuid.UUID, AllowedTestRecipient] = {}
    ids = list(dict.fromkeys(recipient_ids))
    if ids:
        rows = list(
            await session.scalars(
                select(AllowedTestRecipient).where(AllowedTestRecipient.id.in_(ids))
            )
        )
        by_id = {row.id: row for row in rows}
        missing = [str(recipient_id) for recipient_id in ids if recipient_id not in by_id]
        if missing:
            raise NotFound(
                "Some test recipients were not found",
                code="test_recipient_not_found",
                details={"missing_ids": missing},
            )

    by_email: dict[str, AllowedTestRecipient] = {}
    emails = list(dict.fromkeys(email.strip().lower() for email in recipient_emails))
    if emails:
        rows = list(
            await session.scalars(
                select(AllowedTestRecipient).where(
                    func.lower(AllowedTestRecipient.recipient_email).in_(emails)
                )
            )
        )
        by_email = {row.recipient_email.strip().lower(): row for row in rows}
        missing = [email for email in emails if email not in by_email]
        if missing:
            raise PolicyViolation(
                "Every test destination must first be added to the verified allowlist",
                code="test_recipient_not_allowed",
                details={"missing_emails": missing},
            )

    resolved: list[AllowedTestRecipient] = []
    seen: set[uuid.UUID] = set()
    for recipient_id in ids:
        recipient = by_id[recipient_id]
        if recipient.id not in seen:
            resolved.append(recipient)
            seen.add(recipient.id)
    for email in emails:
        recipient = by_email[email]
        if recipient.id not in seen:
            resolved.append(recipient)
            seen.add(recipient.id)

    disabled = [row.recipient_email for row in resolved if not row.is_enabled]
    if disabled:
        raise PolicyViolation(
            "Disabled recipients cannot receive tests",
            code="test_recipient_disabled",
            details={"recipients": disabled},
        )
    unverified = [row.recipient_email for row in resolved if row.verified_at is None]
    if unverified:
        raise PolicyViolation(
            "Every test recipient must be verified",
            code="test_recipient_unverified",
            details={"recipients": unverified},
        )
    return resolved


async def _results_map(
    session: AsyncSession, run_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[TestRunResult]]:
    ids = list(run_ids)
    if not ids:
        return {}
    rows = list(
        await session.scalars(
            select(TestRunResult)
            .where(TestRunResult.test_run_id.in_(ids))
            .order_by(TestRunResult.test_run_id, TestRunResult.id)
        )
    )
    grouped: dict[uuid.UUID, list[TestRunResult]] = {run_id: [] for run_id in ids}
    for row in rows:
        grouped.setdefault(row.test_run_id, []).append(row)
    return grouped


@router.post("", status_code=201)
async def create_test_run(
    payload: TestRunCreateRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    account = await session.get(EmailAccount, payload.account_id)
    if account is None:
        raise NotFound("Account not found")
    if account.state == "disabled":
        raise PolicyViolation("Account is disabled", code="account_disabled")

    recipients = await _resolve_recipients(
        session,
        recipient_ids=payload.recipient_ids,
        recipient_emails=[str(email) for email in payload.recipient_emails],
    )
    if len(recipients) > 20:
        raise PolicyViolation(
            "A single test run is limited to 20 recipients",
            code="too_many_test_recipients",
        )

    if await session.get(SendPolicy, account.id) is None:
        raise PolicyViolation("Send policy is missing", code="policy_missing")

    run = await test_service.create_run(
        session,
        account=account,
        recipients=recipients,
        profile=payload.profile,
        actor=principal.actor,
        **_context(request, principal),
    )
    await session.commit()

    results = await test_service.results_for_run(session, run.id)
    return {
        "test_run": test_run_payload(run, results),
        "note": (
            "Queued against the sending worker's normal quota and safety checks. "
            "A provider message id means 'accepted', not 'delivered' or 'inbox'. "
            "Record the received headers to establish inbox placement."
        ),
    }


@router.get("")
async def list_test_runs(
    account_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    statement = select(TestRun).order_by(TestRun.created_at.desc(), TestRun.id.desc())
    if account_id is not None:
        statement = statement.where(TestRun.account_id == account_id)
    if status:
        statement = statement.where(TestRun.status == status)
    rows = list(await session.scalars(statement.offset(offset).limit(limit + 1)))
    has_more = len(rows) > limit
    rows = rows[:limit]
    grouped = await _results_map(session, (row.id for row in rows))
    return {
        "test_runs": [
            test_run_payload(row, grouped.get(row.id, []))
            for row in rows
        ],
        "has_more": has_more,
        "next_offset": offset + len(rows) if has_more else None,
    }


@router.get("/{run_id}")
async def get_test_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    run = await session.get(TestRun, run_id)
    if run is None:
        raise NotFound("Test run not found")
    results = await test_service.results_for_run(session, run_id)
    return {"test_run": test_run_payload(run, results)}


async def _get_run(session: AsyncSession, run_id: uuid.UUID) -> TestRun:
    run = await session.get(TestRun, run_id)
    if run is None:
        raise NotFound("Test run not found")
    return run


@router.post("/{run_id}/cancel")
async def cancel_test_run(
    run_id: uuid.UUID,
    payload: CancelRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    run = await _get_run(session, run_id)
    await test_service.cancel_run(
        session, run_id=run.id, actor=principal.actor, reason=payload.reason
    )
    await session.commit()
    results = await test_service.results_for_run(session, run.id)
    return {
        "test_run": test_run_payload(run, results),
        "note": "Queued tests were cancelled. Messages already accepted by a provider cannot be recalled.",
    }


@router.post("/{run_id}/results/{result_id}/observation")
async def record_test_observation(
    run_id: uuid.UUID,
    result_id: uuid.UUID,
    payload: ObservationRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    run = await _get_run(session, run_id)
    if run.status == "cancelled":
        raise Conflict(
            "Observations cannot be added to a cancelled test run",
            code="test_run_cancelled",
        )

    await test_service.record_observation(
        session,
        run_id=run.id,
        result_id=result_id,
        raw_headers=payload.raw_headers,
        observed_inbox=payload.observed_inbox,
        observed_at=payload.observed_at,
        actor=principal.actor,
        **_context(request, principal),
    )
    await session.commit()

    results = await test_service.results_for_run(session, run.id)
    refreshed = await session.get(TestRun, run.id)
    assert refreshed is not None
    return {
        "test_run": test_run_payload(refreshed, results),
        "note": (
            "Placement is now based on the observed message. Authentication verdicts "
            "were parsed from the supplied headers, not inferred from provider acceptance."
        ),
    }
