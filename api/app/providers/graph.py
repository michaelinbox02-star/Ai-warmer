"""Microsoft Graph adapter (`/me/sendMail`).

Graph returns 202 Accepted with no message identifier, so ambiguous transport
failures are reported to the operator rather than blindly retried. A stable
`client-request-id` is sent to support provider-side reconciliation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings
from app.core.mime import html_to_text
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
from app.providers.oauth import OAuthError, refresh_microsoft, token_is_fresh

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.microsoft.com/v1.0"


class GraphProvider(EmailProvider):
    name = "microsoft_graph"
    capabilities = ProviderCapabilities(
        send=True,
        delivery_events=False,
        inbox_read=False,
        oauth=True,
        notes=(
            "Sends with delegated Mail.Send via /me/sendMail.",
            "Graph returns 202 without a message id; delivery state requires provider events from elsewhere.",
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

        tokens = await refresh_microsoft(str(refresh_token))
        return tokens.access_token, tokens.as_secret_update(secret)

    def _auth_failure(self, exc: OAuthError, submitted_at: datetime) -> ProviderSubmissionResult:
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
                    f"{GRAPH_API}/me",
                    params={"$select": "mail,userPrincipalName,displayName"},
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

        mailbox = str(profile.get("mail") or profile.get("userPrincipalName") or "")
        if mailbox and mailbox.lower() != ctx.mailbox_email.lower():
            return ConnectionTestResult(
                ok=False,
                stage="identity",
                code="mailbox_mismatch",
                summary=(
                    "The authorized Microsoft account does not match the configured mailbox "
                    f"({mask_email(ctx.mailbox_email)})."
                ),
                capabilities=self.capabilities,
                details={"authorized_mailbox": mask_email(mailbox)},
            )

        return ConnectionTestResult(
            ok=True,
            stage="complete",
            code="ok",
            summary="Microsoft Graph token refresh and profile lookup succeeded.",
            capabilities=self.capabilities,
            details={"authorized_mailbox": mask_email(mailbox) if mailbox else None},
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

        if message.html_body:
            body_content = message.html_body
            content_type = "HTML"
        else:
            body_content = message.text_body or html_to_text(message.html_body or "")
            content_type = "Text"

        payload: dict[str, Any] = {
            "message": {
                "subject": message.subject,
                "body": {"contentType": content_type, "content": body_content},
                "toRecipients": [{"emailAddress": {"address": message.to_email}}],
            },
            "saveToSentItems": True,
        }
        if message.reply_to_email:
            payload["message"]["replyTo"] = [{"emailAddress": {"address": message.reply_to_email}}]
        if message.from_email.lower() != ctx.mailbox_email.lower():
            payload["message"]["from"] = {"emailAddress": {"address": message.from_email}}

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "client-request-id": str(message.message_id),
            "return-client-request-id": "true",
        }

        try:
            async with httpx.AsyncClient(timeout=settings.worker_send_timeout_seconds) as client:
                response = await client.post(
                    f"{GRAPH_API}/me/sendMail", headers=headers, json=payload
                )
        except httpx.HTTPError as exc:
            classified = classify_transport_error(exc)
            return ProviderSubmissionResult(
                accepted=False,
                retryability=classified.retryability,
                submitted_at=submitted_at,
                diagnostic_code=classified.diagnostic_code,
                detail=classified.detail,
                metadata={
                    "transport": "microsoft_graph",
                    "client_request_id": str(message.message_id),
                },
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
                metadata={
                    "provider_error_code": code,
                    "client_request_id": str(message.message_id),
                    "transport": "microsoft_graph",
                },
                credentials_update=update,
                circuit_trigger=classified.circuit_trigger,
            )

        return ProviderSubmissionResult(
            accepted=True,
            retryability="transient",
            submitted_at=submitted_at,
            provider_message_id=None,
            http_status=response.status_code,
            diagnostic_code="accepted",
            detail=(
                "Microsoft Graph accepted the message (202). Graph returns no message id; "
                "this is not delivery evidence."
            ),
            metadata={
                "client_request_id": str(message.message_id),
                "provider_request_id": response.headers.get("request-id"),
                "transport": "microsoft_graph",
                "provider_message_id_source": "none",
            },
            credentials_update=update,
        )
