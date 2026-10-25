"""Account lifecycle, connection tests, domain checks, and OAuth.

Secrets are written to the configured secret store and never returned.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import Conflict, NotFound, PolicyViolation
from app.core.time import iso, utcnow
from app.db import get_session
from app.models import (
    DomainAuthCheck,
    EmailAccount,
    EmailMessage,
    OAuthFlow,
    SendPolicy,
)
from app.providers.base import ProviderContext
from app.providers.oauth import OAuthError, exchange_google_code, exchange_microsoft_code
from app.providers.registry import provider_for_transport
from app.schemas import (
    AccountCreateRequest,
    AccountDisableRequest,
    AcknowledgementRequest,
    CircuitResetRequest,
    DomainCheckRequest,
    ModeUpdateRequest,
    PolicyUpdateRequest,
    SenderIdentityRequest,
    SmtpCredentialUpdateRequest,
)
from app.security import Principal, client_key, optional_admin, require_admin
from app.serializers import account_payload, domain_check_payload
from app.services import policy as policy_service
from app.services import settings as app_settings
from app.services.audit import record_audit, record_safety_event
from app.services.credentials import (
    build_context,
    delete_credentials,
    get_credential,
    mark_validated,
    persist_token_refresh,
    store_credentials,
)
from app.services.domain_checks import run_checks
from app.services.netguard import resolve_and_validate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts", tags=["accounts"])

GOOGLE_SCOPES = ["openid", "email", "https://www.googleapis.com/auth/gmail.send"]
MICROSOFT_SCOPES = [
    "openid",
    "email",
    "offline_access",
    "https://graph.microsoft.com/Mail.Send",
]


def _audit(request: Request, principal: Principal) -> dict[str, Any]:
    return {
        "request_id": principal.request_id,
        "source_ip": request.client.host if request.client else None,
    }


async def _get_account(session: AsyncSession, account_id: uuid.UUID) -> EmailAccount:
    account = await session.get(EmailAccount, account_id)
    if account is None:
        raise NotFound("Account not found")
    return account


async def _assert_account_capacity(session: AsyncSession) -> None:
    total = await session.scalar(select(func.count()).select_from(EmailAccount))
    if int(total or 0) >= settings.max_accounts:
        raise Conflict(
            f"The configured mailbox limit ({settings.max_accounts}) has been reached",
            code="account_limit_reached",
            details={"limit": settings.max_accounts},
        )


def _ensure_policy_defaults(policy: SendPolicy) -> None:
    policy.mode = "test_only"
    policy.production_enabled = False
    policy.max_per_minute = settings.default_max_per_minute
    policy.max_rolling_24h = settings.default_max_rolling_24h
    policy.max_attempts = settings.default_max_attempts
    policy.max_in_flight = settings.default_max_in_flight


@router.get("")
async def list_accounts(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    accounts = list(
        await session.scalars(select(EmailAccount).order_by(EmailAccount.created_at))
    )
    return {
        "accounts": [
            await account_payload(session, account, include_readiness=True)
            for account in accounts
        ]
    }


@router.post("/smtp", status_code=201)
@router.post("", status_code=201)
async def create_account(
    payload: AccountCreateRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    await _assert_account_capacity(session)
    mailbox = str(payload.mailbox_email).strip()
    existing = await session.scalar(
        select(EmailAccount).where(func.lower(EmailAccount.mailbox_email) == mailbox.lower())
    )
    if existing is not None:
        raise Conflict("An account with this mailbox already exists", code="account_exists")

    if payload.transport == "smtp" and payload.smtp is not None:
        if payload.smtp.port == 465 and payload.smtp.security != "tls":
            raise PolicyViolation(
                "Port 465 requires implicit TLS (security='tls')", code="invalid_smtp_security"
            )
        if payload.smtp.port != 465 and payload.smtp.security == "tls":
            raise PolicyViolation(
                "Implicit TLS is normally used with port 465; use STARTTLS for other ports",
                code="invalid_smtp_security",
            )
        await resolve_and_validate(payload.smtp.host, payload.smtp.port)

    mailbox_domain = mailbox.rsplit("@", 1)[-1].lower()
    sending_domain = payload.sending_domain or mailbox_domain

    metadata: dict[str, Any] = {
        "sender_identity_verified": sending_domain == mailbox_domain,
    }
    if payload.transport == "smtp" and payload.smtp is not None:
        metadata.update(
            {
                "smtp_host": payload.smtp.host.strip().lower(),
                "smtp_port": payload.smtp.port,
                "smtp_security": payload.smtp.security,
                "smtp_username": payload.smtp.username or mailbox,
                "dkim_selectors": payload.dkim_selectors or [],
            }
        )
    if payload.auth_policy:
        metadata["auth_policy"] = payload.auth_policy
    if payload.allowed_from:
        metadata["allowed_from"] = [str(address).lower() for address in payload.allowed_from]

    account = EmailAccount(
        provider=payload.provider,
        transport=payload.transport,
        mailbox_email=mailbox,
        display_name=payload.display_name,
        sending_domain=sending_domain,
        state="pending",
        provider_metadata=metadata,
    )
    session.add(account)
    await session.flush()

    policy = SendPolicy(account_id=account.id)
    _ensure_policy_defaults(policy)
    session.add(policy)

    if payload.transport == "smtp" and payload.smtp is not None:
        await store_credentials(
            session,
            account_id=account.id,
            payload={"password": payload.smtp.password},
            scopes=[],
        )

    await record_audit(
        session,
        actor=principal.actor,
        action="account.create",
        target_type="email_account",
        target_id=account.id,
        details={"provider": account.provider, "transport": account.transport},
        **_audit(request, principal),
    )
    await session.commit()

    return await account_payload(session, account)


@router.get("/{account_id}")
async def get_account(
    account_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    account = await _get_account(session, account_id)
    payload = await account_payload(session, account)
    credential = await get_credential(session, account.id)
    payload["credential_configured"] = credential is not None
    return payload


@router.post("/{account_id}/test-connection")
async def test_connection(
    account_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    account = await _get_account(session, account_id)
    provider = provider_for_transport(account.transport)
    ctx = await build_context(session, account)

    result = await provider.validate_connection(ctx)
    now = utcnow()

    if result.credentials_update:
        await persist_token_refresh(
            session, account_id=account.id, update=result.credentials_update
        )
    account.connection_tested_at = now
    account.updated_at = now
    await mark_validated(session, account_id=account.id)

    if result.ok:
        account.state = "connected"
        account.last_error_code = None
        account.last_error_summary = None
    else:
        account.state = "auth_required" if result.stage == "auth" else "error"
        account.last_error_code = result.code
        account.last_error_summary = result.summary

    await record_audit(
        session,
        actor=principal.actor,
        action="account.test_connection",
        target_type="email_account",
        target_id=account.id,
        result="success" if result.ok else "failure",
        details={
            "stage": result.stage,
            "code": result.code,
            "state": account.state,
        },
        **_audit(request, principal),
    )
    if not result.ok:
        await record_safety_event(
            session,
            severity="warning",
            event_type="connection_test_failed",
            action_taken="account is not ready for production",
            account_id=account.id,
            details={"stage": result.stage, "code": result.code, "summary": result.summary},
        )

    await session.commit()

    return {
        "ok": result.ok,
        "stage": result.stage,
        "code": result.code,
        "summary": result.summary,
        "details": result.details,
        "capabilities": result.capabilities.as_dict(),
        "account_state": account.state,
        "tested_at": iso(now),
    }


@router.post("/{account_id}/domain-checks")
async def run_domain_checks(
    account_id: uuid.UUID,
    payload: DomainCheckRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    account = await _get_account(session, account_id)
    rows = await run_checks(
        session,
        account=account,
        actor=principal.actor,
        check_types=payload.check_types,
        **_audit(request, principal),
    )
    await session.commit()
    return {
        "checks": [domain_check_payload(row) for row in rows],
        "readiness": (
            await policy_service.build_readiness(session, account)
        ).as_dict(),
    }


@router.get("/{account_id}/domain-checks")
async def list_domain_checks(
    account_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    account = await _get_account(session, account_id)
    history = list(
        await session.scalars(
            select(DomainAuthCheck)
            .where(DomainAuthCheck.account_id == account.id)
            .order_by(DomainAuthCheck.checked_at.desc())
            .limit(limit)
        )
    )
    latest = await policy_service.latest_auth_statuses(session, account.id)
    return {
        "latest": latest,
        "history": [domain_check_payload(row) for row in history],
    }


@router.post("/{account_id}/mode")
async def set_mode(
    account_id: uuid.UUID,
    payload: ModeUpdateRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    account = await _get_account(session, account_id)
    policy = await session.get(SendPolicy, account.id)
    if policy is None:
        raise NotFound("Send policy not found", code="policy_missing")

    if payload.mode == "production":
        await policy_service.enable_production(
            session,
            account=account,
            policy=policy,
            actor=principal.actor,
            acknowledgements=payload.acknowledgements,
            request_id=principal.request_id,
            source_ip=request.client.host if request.client else None,
        )
    else:
        previous = policy.mode
        policy.mode = payload.mode
        policy.production_enabled = False
        policy.updated_at = utcnow()
        await record_audit(
            session,
            actor=principal.actor,
            action="account.set_mode",
            target_type="email_account",
            target_id=account.id,
            details={"from": previous, "to": payload.mode, "reason": payload.reason},
            **_audit(request, principal),
        )
        if payload.mode == "paused":
            await record_safety_event(
                session,
                severity="warning",
                event_type="account_paused",
                action_taken="no new submissions will be attempted for this account",
                account_id=account.id,
                details={"actor": principal.actor, "reason": payload.reason},
            )

    await session.commit()
    updated = await account_payload(session, account)
    return {"account": updated, "note": "Readiness is reported in account.readiness."}


@router.post("/{account_id}/policy")
async def update_policy(
    account_id: uuid.UUID,
    payload: PolicyUpdateRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    account = await _get_account(session, account_id)
    policy = await session.get(SendPolicy, account.id)
    if policy is None:
        raise NotFound("Send policy not found", code="policy_missing")

    before = {
        "max_per_minute": policy.max_per_minute,
        "max_rolling_24h": policy.max_rolling_24h,
        "max_attempts": policy.max_attempts,
        "max_in_flight": policy.max_in_flight,
    }
    for field in (
        "max_per_minute",
        "max_rolling_24h",
        "max_attempts",
        "max_in_flight",
        "require_auth_checks",
        "require_test_recipient_allowlist",
    ):
        value = getattr(payload, field)
        if value is not None:
            setattr(policy, field, value)
    policy.updated_at = utcnow()

    snapshot = await policy_service.quota_snapshot(session, account.id, policy)
    warnings: list[str] = []
    if snapshot.rolling_24h_used > policy.max_rolling_24h:
        warnings.append(
            "The rolling 24-hour cap is below current usage; new sends will be blocked until it drains."
        )
    if snapshot.per_minute_used > policy.max_per_minute:
        warnings.append("The per-minute cap is below current usage; new sends will be briefly blocked.")

    await record_audit(
        session,
        actor=principal.actor,
        action="account.policy_update",
        target_type="email_account",
        target_id=account.id,
        details={"before": before, "after": {
            "max_per_minute": policy.max_per_minute,
            "max_rolling_24h": policy.max_rolling_24h,
            "max_attempts": policy.max_attempts,
            "max_in_flight": policy.max_in_flight,
        }, "reason": payload.reason},
        **_audit(request, principal),
    )
    await session.commit()
    return {
        "policy": (await account_payload(session, account))["policy"],
        "quota": snapshot.as_dict(),
        "warnings": warnings,
    }


@router.post("/{account_id}/circuit/reset")
async def reset_circuit(
    account_id: uuid.UUID,
    payload: CircuitResetRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    account = await _get_account(session, account_id)
    policy = await session.get(SendPolicy, account.id)
    if policy is None:
        raise NotFound("Send policy not found", code="policy_missing")

    await policy_service.close_circuit(
        session,
        policy=policy,
        account=account,
        reason=payload.reason,
        actor=principal.actor,
        request_id=principal.request_id,
        source_ip=request.client.host if request.client else None,
    )
    await session.commit()

    warnings = []
    if account.state == "auth_required":
        warnings.append("Credentials are still marked as rejected; re-run the connection test first.")
    return {
        "circuit": (await account_payload(session, account))["policy"]["circuit"],
        "warnings": warnings,
    }


@router.post("/{account_id}/acknowledge-warnings")
async def acknowledge_warnings(
    account_id: uuid.UUID,
    payload: AcknowledgementRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    account = await _get_account(session, account_id)
    await app_settings.acknowledge_warnings(
        session,
        account_id=account.id,
        keys=payload.keys,
        actor=principal.actor,
        request_id=principal.request_id,
        source_ip=request.client.host if request.client else None,
    )
    await session.commit()
    return (await account_payload(session, account))["readiness"]


@router.post("/{account_id}/sender-identity")
async def set_sender_identity(
    account_id: uuid.UUID,
    payload: SenderIdentityRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    account = await _get_account(session, account_id)
    metadata = dict(account.provider_metadata or {})
    metadata["sender_identity_verified"] = bool(payload.verified)
    metadata["sender_identity_note"] = payload.note
    account.provider_metadata = metadata
    account.sending_domain = payload.sending_domain
    account.updated_at = utcnow()

    await record_audit(
        session,
        actor=principal.actor,
        action="account.sender_identity",
        target_type="email_account",
        target_id=account.id,
        details={
            "sending_domain": payload.sending_domain,
            "verified": payload.verified,
            "note": payload.note,
        },
        **_audit(request, principal),
    )
    await session.commit()
    return await account_payload(session, account)


@router.post("/{account_id}/credentials/smtp")
async def update_smtp_credentials(
    account_id: uuid.UUID,
    payload: SmtpCredentialUpdateRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    account = await _get_account(session, account_id)
    if account.transport != "smtp":
        raise PolicyViolation(
            "This endpoint only updates SMTP passwords; use OAuth for API transports",
            code="not_smtp_account",
        )

    metadata = dict(account.provider_metadata or {})
    if payload.username is not None:
        metadata["smtp_username"] = payload.username.strip() or account.mailbox_email
    account.provider_metadata = metadata
    await store_credentials(
        session,
        account_id=account.id,
        payload={"password": payload.password},
        scopes=[],
    )

    # A changed password is unverified until the operator runs a connection
    # test. Keep production mode configured but block the worker meanwhile.
    account.state = "pending"
    account.last_error_code = None
    account.last_error_summary = None
    account.connection_tested_at = None
    account.updated_at = utcnow()

    await record_audit(
        session,
        actor=principal.actor,
        action="account.smtp_credentials_update",
        target_type="email_account",
        target_id=account.id,
        details={"username_changed": payload.username is not None},
        **_audit(request, principal),
    )
    await record_safety_event(
        session,
        severity="info",
        event_type="smtp_credentials_rotated",
        action_taken="account set to pending until the connection test succeeds",
        account_id=account.id,
        details={"actor": principal.actor},
    )
    await session.commit()
    return {
        "account": await account_payload(session, account),
        "note": "The new password is stored in the secret store. Run a connection test now.",
    }


@router.post("/{account_id}/disable")
async def disable_account(
    account_id: uuid.UUID,
    payload: AccountDisableRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    account = await _get_account(session, account_id)
    policy = await session.get(SendPolicy, account.id)
    account.state = "disabled"
    account.updated_at = utcnow()
    if policy is not None:
        policy.mode = "paused"
        policy.production_enabled = False
        policy.updated_at = utcnow()

    await record_audit(
        session,
        actor=principal.actor,
        action="account.disable",
        target_type="email_account",
        target_id=account.id,
        details={"reason": payload.reason},
        **_audit(request, principal),
    )
    await record_safety_event(
        session,
        severity="warning",
        event_type="account_disabled",
        action_taken="no further submissions",
        account_id=account.id,
        details={"reason": payload.reason, "actor": principal.actor},
    )
    await session.commit()
    return {"disabled": True, "account": await account_payload(session, account)}


@router.delete("/{account_id}")
async def delete_account(
    account_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    account = await _get_account(session, account_id)
    message_count = await session.scalar(
        select(func.count()).select_from(EmailMessage).where(EmailMessage.account_id == account.id)
    )
    if message_count:
        raise Conflict(
            "Account has message history and cannot be deleted. Disable it instead.",
            code="account_has_history",
            details={"messages": int(message_count)},
        )

    await delete_credentials(session, account_id=account.id)
    await session.delete(account)
    await record_audit(
        session,
        actor=principal.actor,
        action="account.delete",
        target_type="email_account",
        target_id=account_id,
        **_audit(request, principal),
    )
    await session.commit()
    return {"deleted": True, "id": str(account_id)}


# ---------------------------------------------------------------------------
# OAuth (PKCE + single-use state)
# ---------------------------------------------------------------------------


def _redirect_uri(provider: str) -> str:
    return f"{settings.public_base_url}/api/v1/accounts/oauth/{provider}/callback"


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    return verifier, challenge


def _infer_provider(family: str, mailbox: str) -> str:
    domain = mailbox.rsplit("@", 1)[-1].lower()
    if family == "google":
        return "google_gmail" if domain in {"gmail.com", "googlemail.com"} else "google_workspace"
    return "outlook_com" if domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"} else "microsoft_365"


@router.post("/oauth/{provider}/start")
async def start_oauth(
    provider: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    if provider not in {"google_gmail", "google_workspace", "microsoft_365", "outlook_com"}:
        raise PolicyViolation("Unsupported OAuth provider", code="invalid_oauth_provider")

    family = "google" if provider.startswith("google") else "microsoft"
    if family == "google":
        if not settings.google_client_id or not settings.google_client_secret:
            raise PolicyViolation(
                "Google OAuth is not configured (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET)",
                code="oauth_not_configured",
            )
    else:
        if not settings.microsoft_client_id:
            raise PolicyViolation(
                "Microsoft OAuth is not configured (MICROSOFT_CLIENT_ID)",
                code="oauth_not_configured",
            )

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    redirect_uri = _redirect_uri(provider)
    scopes = GOOGLE_SCOPES if family == "google" else MICROSOFT_SCOPES

    flow = OAuthFlow(
        state=state,
        provider=provider,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
        requested_scopes=scopes,
        expires_at=utcnow() + timedelta(minutes=10),
    )
    session.add(flow)

    if family == "google":
        params = {
            "client_id": settings.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        authorize_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    else:
        params = {
            "client_id": settings.microsoft_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "response_mode": "query",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        authorize_url = (
            f"https://login.microsoftonline.com/{settings.microsoft_tenant}/oauth2/v2.0/authorize?"
            + urlencode(params)
        )

    await record_audit(
        session,
        actor=principal.actor,
        action="account.oauth_start",
        target_type="oauth_flow",
        details={"provider": provider},
        **_audit(request, principal),
    )
    await session.commit()

    return {
        "authorize_url": authorize_url,
        "state": state,
        "expires_at": iso(flow.expires_at),
        "redirect_uri": redirect_uri,
        "scopes": scopes,
    }


async def _oauth_mailbox(family: str, access_token: str) -> str | None:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        if family == "google":
            response = await client.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/profile", headers=headers
            )
            if response.status_code >= 400:
                return None
            return str(response.json().get("emailAddress") or "") or None
        response = await client.get(
            "https://graph.microsoft.com/v1.0/me",
            params={"$select": "mail,userPrincipalName"},
            headers=headers,
        )
        if response.status_code >= 400:
            return None
        data = response.json()
        return str(data.get("mail") or data.get("userPrincipalName") or "") or None


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    principal: Principal | None = Depends(optional_admin),
) -> RedirectResponse:
    def failure(reason: str) -> RedirectResponse:
        return RedirectResponse(
            f"{settings.public_base_url}/accounts?oauth_error={reason}", status_code=303
        )

    if principal is None:
        return RedirectResponse(f"{settings.public_base_url}/login", status_code=303)
    if error:
        logger.warning("OAuth provider returned an error: %s", error)
        return failure(error)
    if not code or not state:
        return failure("missing_code_or_state")

    flow = await session.scalar(
        select(OAuthFlow).where(OAuthFlow.state == state).with_for_update()
    )
    if flow is None or flow.provider != provider:
        return failure("invalid_state")
    if flow.consumed_at is not None:
        return failure("state_already_used")
    if flow.expires_at <= utcnow():
        return failure("state_expired")

    flow.consumed_at = utcnow()
    family = "google" if provider.startswith("google") else "microsoft"

    try:
        tokens = (
            await exchange_google_code(code, flow.code_verifier, flow.redirect_uri)
            if family == "google"
            else await exchange_microsoft_code(code, flow.code_verifier, flow.redirect_uri)
        )
    except OAuthError as exc:
        logger.warning("OAuth code exchange failed: %s", exc.code)
        return failure(exc.code)

    if not tokens.refresh_token:
        return failure("missing_refresh_token")

    try:
        mailbox = await _oauth_mailbox(family, tokens.access_token)
    except httpx.HTTPError:
        return failure("profile_lookup_failed")
    if not mailbox:
        return failure("profile_lookup_failed")

    resolved_provider = _infer_provider(family, mailbox)
    account = await session.scalar(
        select(EmailAccount).where(func.lower(EmailAccount.mailbox_email) == mailbox.lower())
    )
    transport = "gmail_api" if family == "google" else "microsoft_graph"

    if account is not None and account.transport != transport:
        return failure("account_transport_mismatch")

    metadata_extra = {
        "oauth_family": family,
        "oauth_scopes": list(tokens.scopes),
        "sender_identity_verified": True,
    }

    if account is None:
        try:
            await _assert_account_capacity(session)
        except Conflict:
            return failure("account_limit_reached")
        account = EmailAccount(
            provider=resolved_provider,
            transport=transport,
            mailbox_email=mailbox,
            sending_domain=mailbox.rsplit("@", 1)[-1].lower(),
            state="pending",
            provider_metadata=metadata_extra,
        )
        session.add(account)
        await session.flush()
        policy = SendPolicy(account_id=account.id)
        _ensure_policy_defaults(policy)
        session.add(policy)
    else:
        account.provider = resolved_provider
        account.transport = transport
        account.updated_at = utcnow()
        if not account.sending_domain:
            account.sending_domain = mailbox.rsplit("@", 1)[-1].lower()
        account.provider_metadata = {**(account.provider_metadata or {}), **metadata_extra}

    await store_credentials(
        session,
        account_id=account.id,
        payload={
            "refresh_token": tokens.refresh_token,
            "access_token": tokens.access_token,
            "expires_at": tokens.expires_at.isoformat(),
            "token_type": tokens.token_type,
            "scopes": list(tokens.scopes),
        },
        scopes=list(tokens.scopes),
        access_token_expires_at=tokens.expires_at,
    )

    await record_audit(
        session,
        actor=principal.actor,
        action="account.oauth_connect",
        target_type="email_account",
        target_id=account.id,
        details={"provider": account.provider, "mailbox": mailbox},
        **_audit(request, principal),
    )
    await session.commit()

    # Best-effort immediate validation; the operator can re-test at any time.
    try:
        ctx = await build_context(session, account)
        result = await provider_for_transport(account.transport).validate_connection(ctx)
        account.connection_tested_at = utcnow()
        if result.ok:
            account.state = "connected"
            account.last_error_code = None
            account.last_error_summary = None
        else:
            account.state = "auth_required" if result.stage == "auth" else "error"
            account.last_error_code = result.code
            account.last_error_summary = result.summary
        await session.commit()
    except Exception:  # noqa: BLE001 - never block the redirect on validation
        await session.rollback()
        logger.warning("Post-OAuth connection validation failed", exc_info=True)

    return RedirectResponse(
        f"{settings.public_base_url}/accounts/{account.id}?connected=1", status_code=303
    )
