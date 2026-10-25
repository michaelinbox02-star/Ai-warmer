"""Gmail API adapter (`users.messages.send`).

Send-only by default. Acceptance is recorded as `submitted`; Gmail does not
give this application delivery evidence, and inbox placement is never inferred.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings
from app.core.mime import build_message
from app.core.policy_rules import Retryability
from app.core.redaction import mask_email, sanitize_text
from app.providers.base import (
    ConnectionTestResult,
    CredentialRefreshResult,
    EmailProvider,
    OutboundMessage,
    ProviderCapabilities,
    ProviderContext,
    ProviderSubmissionResult,
)
from app.providers.http_common import classify_http_error, classify_transport_error, error_body
from app.providers.oauth import OAuthError, refresh_google, token_is_fresh

logger = logging.getLogger(__name__)

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"


class GmailProvider(EmailProvider):
    name = "gmail_api"
    capabilities = ProviderCapabilities(
        send=True,
        delivery_events=False,
        inbox_read=False,
        oauth=True,
        notes=(
            "Sends through the Gmail API with the gmail.send scope only.",
            "Google does not expose per-message delivery receipts here; accepted messages stay 'submitted'.",
        ),
    )

    async def _token(self, ctx: ProviderContext) -> tuple[str, dict[str, Any] | None]:
        secret = dict(ctx.secret)
        existing = secret.get("access_token")
        if existing and token_is_fresh(secret):
            return str(existing), None

        refresh_token = secret.get("refresh_token")
        if not refresh_token:
            raise OAuthError("missing_refresh_token", "No refresh token is stored for this account")

        tokens = await refresh_google(str(refresh_token))
        return tokens.access_token, tokens.as_secret_update(secret)

    def _auth_failure(
        self, exc: OAuthError, submitted_at: datetime
    ) -> ProviderSubmissionResult:
        return ProviderSubmissionResult(
            accepted=False,
            retryability="transient" if exc.retryable else "auth",
            submitted_at=submitted_at,
            diagnostic_code=exc.code,
            detail=exc.description,
            circuit_trigger=None if exc.retryable else "auth_failure",
        )

    async def validate_connection(self, ctx: ProviderContext) -> ConnectionTestResult:
        try:
            token, update = await self._token(ctx)
        except OAuthError as exc:
            return ConnectionTestResult(
                ok=False,
                stage="auth",
                code=exc.code,
                summary=exc.description,
                capabilities=self.capabilities,
            )

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(
                    f"{GMAIL_API}/users/me/profile",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            classified = classify_transport_error(exc)
            return ConnectionTestResult(
                ok=False,
                stage="connect",
                code=classified.diagnostic_code,
                summary=classified.detail,
                capabilities=self.capabilities,
            )

        if response.status_code >= 400:
            code, message = error_body(response)
            classified = classify_http_error(response, provider_code=code, message=message)
            return ConnectionTestResult(
                ok=False,
                stage="api",
                code=classified.diagnostic_code,
                summary=classified.detail,
                capabilities=self.capabilities,
            )

        try:
            profile = response.json()
        except ValueError:
            profile = {}

        mailbox = str(profile.get("emailAddress") or "")
        if mailbox and mailbox.lower() != ctx.mailbox_email.lower():
            return ConnectionTestResult(
                ok=False,
                stage="identity",
                code="mailbox_mismatch",
                summary=(
                    "The authorized Google account does not match the configured mailbox "
                    f"({mask_email(ctx.mailbox_email)})."
                ),
                capabilities=self.capabilities,
                details={"authorized_mailbox": mask_email(mailbox)},
            )

        return ConnectionTestResult(
            ok=True,
            stage="complete",
            code="ok",
            summary="Gmail API token refresh and profile lookup succeeded.",
            capabilities=self.capabilities,
            details={
                "authorized_mailbox": mask_email(mailbox) if mailbox else None,
                "messages_total": profile.get("messagesTotal"),
            },
            credentials_update=update,
        )

    async def refresh_credentials(self, ctx: ProviderContext) -> CredentialRefreshResult:
        try:
            _, update = await self._token(ctx)
        except OAuthError as exc:
            return CredentialRefreshResult(ok=False, code=exc.code, summary=exc.description)
        return CredentialRefreshResult(
            ok=True,
            code="refreshed",
            summary="Access token refreshed.",
            credentials_update=update,
        )

    async def send(self, ctx: ProviderContext, message: OutboundMessage) -> ProviderSubmissionResult:
        submitted_at = datetime.now(timezone.utc)
        try:
            token, update = await self._token(ctx)
        except OAuthError as exc:
            return self._auth_failure(exc, submitted_at)

        try:
            raw_bytes = build_message(
                message,
                extra_headers=(
                    [("X-Deliverability-Test", "controlled")]
                    if message.kind == "diagnostic_test"
                    else []
                ),
                max_bytes=settings.max_message_bytes,
            )
        except Exception as exc:  # message construction problems are permanent
            return ProviderSubmissionResult(
                accepted=False,
                retryability="permanent",
                submitted_at=submitted_at,
                diagnostic_code="message_build_failed",
                detail=sanitize_text(str(exc), limit=300),
                credentials_update=update,
            )

        encoded = base64.urlsafe_b64encode(raw_bytes).decode("ascii")
        body: dict[str, Any] = {"raw": encoded}

        try:
            async with httpx.AsyncClient(timeout=settings.worker_send_timeout_seconds) as client:
                response = await client.post(
                    f"{GMAIL_API}/users/me/messages/send",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
        except httpx.HTTPError as exc:
            classified = classify_transport_error(exc)
            return ProviderSubmissionResult(
                accepted=False,
                retryability=classified.retryability,
                submitted_at=submitted_at,
                diagnostic_code=classified.diagnostic_code,
                detail=classified.detail,
                metadata={"transport": "gmail_api"},
                credentials_update=update,
            )

        if response.status_code >= 400:
            code, detail = error_body(response)
            classified = classify_http_error(response, provider_code=code, message=detail)
            return ProviderSubmissionResult(
                accepted=False,
                retryability=classified.retryability,
                submitted_at=submitted_at,
                http_status=response.status_code,
                diagnostic_code=classified.diagnostic_code,
                detail=classified.detail,
                retry_after_seconds=classified.retry_after_seconds,
                metadata={"provider_error_code": code, "transport": "gmail_api"},
                credentials_update=update,
                circuit_trigger=classified.circuit_trigger,
            )

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        provider_message_id = str(payload.get("id") or "") or None
        return ProviderSubmissionResult(
            accepted=True,
            retryability="transient",
            submitted_at=submitted_at,
            provider_message_id=provider_message_id,
            http_status=response.status_code,
            diagnostic_code="accepted",
            detail="Gmail accepted the message for delivery. This is not delivery evidence.",
            metadata={
                "thread_id": payload.get("threadId"),
                "label_ids": payload.get("labelIds", []),
                "transport": "gmail_api",
                "provider_message_id_source": "gmail_api",
            },
            credentials_update=update,
        )
