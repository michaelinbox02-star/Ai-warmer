"""Provider submission outcomes using mocked HTTP transports.

These tests assert the honest reporting rules: provider acceptance is recorded
as accepted/submitted and never as delivery or inbox placement.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.providers.base import OutboundMessage, ProviderContext
from app.providers.gmail import GMAIL_API, GmailProvider
from app.providers.graph import GRAPH_API, GraphProvider

MESSAGE_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
NOW = datetime.now(timezone.utc)


def context(secret: dict | None = None) -> ProviderContext:
    return ProviderContext(
        account_id=uuid.UUID("44444444-4444-4444-4444-444444444444"),
        provider="google_gmail",
        transport="gmail_api",
        mailbox_email="sender@example.com",
        display_name="Sender",
        sending_domain="example.com",
        metadata={},
        secret=secret
        or {
            "access_token": "ya29.valid-token",
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "refresh_token": "refresh-token",
        },
    )


def message(**overrides) -> OutboundMessage:
    values = {
        "message_id": MESSAGE_ID,
        "from_email": "sender@example.com",
        "to_email": "recipient@example.net",
        "subject": "Controlled test",
        "text_body": "Hello",
        "html_body": None,
        "kind": "diagnostic_test",
    }
    values.update(overrides)
    return OutboundMessage(**values)


@respx.mock
async def test_gmail_acceptance_records_message_id_but_not_delivery():
    route = respx.post(f"{GMAIL_API}/users/me/messages/send").mock(
        return_value=httpx.Response(
            200, json={"id": "18c2f0", "threadId": "18c2f0", "labelIds": ["SENT"]}
        )
    )
    result = await GmailProvider().send(context(), message())

    assert route.called
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer ya29.valid-token"
    payload = request.read()
    assert b'"raw"' in payload

    assert result.accepted is True
    assert result.provider_message_id == "18c2f0"
    assert result.http_status == 200
    assert result.diagnostic_code == "accepted"
    assert result.circuit_trigger is None
    assert "not delivery evidence" in (result.detail or "")
    assert result.metadata["transport"] == "gmail_api"


@respx.mock
async def test_gmail_rate_limit_carries_retry_hint_and_circuit_trigger():
    respx.post(f"{GMAIL_API}/users/me/messages/send").mock(
        return_value=httpx.Response(
            429,
            headers={"retry-after": "45"},
            json={
                "error": {
                    "code": 429,
                    "message": "User-rate limit exceeded. token=super-secret",
                    "status": "RESOURCE_EXHAUSTED",
                }
            },
        )
    )
    result = await GmailProvider().send(context(), message())

    assert result.accepted is False
    assert result.retryability == "rate_limited"
    assert result.retry_after_seconds == 45
    assert result.http_status == 429
    assert result.circuit_trigger == "quota_restriction"
    assert "super-secret" not in (result.detail or "")
    assert result.provider_message_id is None


@respx.mock
async def test_gmail_transport_failure_before_connect_is_transient():
    respx.post(f"{GMAIL_API}/users/me/messages/send").mock(
        side_effect=httpx.ConnectError("refused", request=httpx.Request("POST", "https://x"))
    )
    result = await GmailProvider().send(context(), message())
    assert result.accepted is False
    assert result.retryability == "transient"
    assert result.metadata["transport"] == "gmail_api"


@respx.mock
async def test_graph_accepts_without_a_message_id():
    route = respx.post(f"{GRAPH_API}/me/sendMail").mock(
        return_value=httpx.Response(202, headers={"request-id": "graph-request"})
    )
    graph_context = ProviderContext(
        account_id=uuid.UUID("55555555-5555-5555-5555-555555555555"),
        provider="microsoft_365",
        transport="microsoft_graph",
        mailbox_email="sender@example.com",
        display_name="Sender",
        sending_domain="example.com",
        metadata={},
        secret=context().secret,
    )
    result = await GraphProvider().send(graph_context, message())

    assert route.called
    request = route.calls[0].request
    assert request.headers["client-request-id"] == str(MESSAGE_ID)
    assert request.headers["return-client-request-id"] == "true"

    assert result.accepted is True
    assert result.provider_message_id is None
    assert result.http_status == 202
    assert result.metadata["provider_request_id"] == "graph-request"
    assert result.metadata["provider_message_id_source"] == "none"
    assert "not delivery evidence" in (result.detail or "")


@respx.mock
async def test_graph_auth_failure_opens_the_circuit():
    respx.post(f"{GRAPH_API}/me/sendMail").mock(
        return_value=httpx.Response(
            401, json={"error": "invalid_token", "error_description": "expired"}
        )
    )
    result = await GraphProvider().send(context(), message())
    assert result.accepted is False
    assert result.retryability == "auth"
    assert result.circuit_trigger == "auth_failure"
    assert result.diagnostic_code == "invalid_token"


@respx.mock
async def test_missing_refresh_token_is_reported_without_a_call():
    route = respx.post(f"{GMAIL_API}/users/me/messages/send").mock(
        return_value=httpx.Response(200, json={"id": "x"})
    )
    result = await GmailProvider().send(
        context(secret={"access_token": "expired", "expires_at": (NOW - timedelta(hours=1)).isoformat()}),
        message(),
    )
    assert result.accepted is False
    assert result.retryability == "auth"
    assert result.diagnostic_code == "missing_refresh_token"
    assert route.called is False
