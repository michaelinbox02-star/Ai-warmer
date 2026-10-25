"""OAuth token exchange helpers with mocked endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.config import settings
from app.providers.oauth import (
    GOOGLE_TOKEN_URL,
    OAuthError,
    exchange_google_code,
    exchange_microsoft_code,
    microsoft_token_url,
    refresh_google,
    refresh_microsoft,
    token_is_fresh,
)


@pytest.fixture()
def google_configured(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "google-client")
    monkeypatch.setattr(settings, "google_client_secret", "google-secret")


@pytest.fixture()
def microsoft_configured(monkeypatch):
    monkeypatch.setattr(settings, "microsoft_client_id", "ms-client")
    monkeypatch.setattr(settings, "microsoft_client_secret", "ms-secret")
    monkeypatch.setattr(settings, "microsoft_tenant", "common")


def token_payload(**overrides):
    values = {
        "access_token": "new-access-token",
        "expires_in": 3600,
        "token_type": "Bearer",
        "refresh_token": "new-refresh-token",
        "scope": "https://mail.google.com/ openid email",
    }
    values.update(overrides)
    return values


def test_token_is_fresh_honours_skew_and_timezone():
    assert token_is_fresh({"expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()})
    assert not token_is_fresh({"expires_at": (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()})
    assert token_is_fresh(
        {"expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).replace(tzinfo=None).isoformat()}
    )
    assert not token_is_fresh({})
    assert not token_is_fresh({"expires_at": "not-a-date"})


@respx.mock
async def test_google_refresh_sends_credentials_and_parses_tokens(google_configured):
    route = respx.post(GOOGLE_TOKEN_URL).mock(
        return_value=httpx.Response(200, json=token_payload())
    )
    tokens = await refresh_google("refresh-token")

    request = route.calls[0].request
    body = request.read().decode()
    assert "grant_type=refresh_token" in body
    assert "refresh_token=refresh-token" in body
    assert "client_id=google-client" in body
    assert "client_secret=google-secret" in body

    assert tokens.access_token == "new-access-token"
    assert tokens.refresh_token == "new-refresh-token"
    assert tokens.scopes == ("email", "https://mail.google.com/", "openid")
    assert tokens.expires_at > datetime.now(timezone.utc)


@respx.mock
async def test_google_refresh_requires_configuration(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")
    with pytest.raises(OAuthError) as excinfo:
        await refresh_google("refresh-token")
    assert excinfo.value.code == "google_oauth_not_configured"


@respx.mock
async def test_google_authorization_code_exchange_uses_pkce_verifier(google_configured):
    route = respx.post(GOOGLE_TOKEN_URL).mock(
        return_value=httpx.Response(200, json=token_payload())
    )
    await exchange_google_code("auth-code", "verifier-value", "http://localhost:3000/callback")
    body = route.calls[0].request.read().decode()
    assert "grant_type=authorization_code" in body
    assert "code=auth-code" in body
    assert "code_verifier=verifier-value" in body
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A3000%2Fcallback" in body


@respx.mock
async def test_microsoft_refresh_uses_tenant_and_scope(microsoft_configured):
    route = respx.post(microsoft_token_url()).mock(
        return_value=httpx.Response(200, json=token_payload())
    )
    tokens = await refresh_microsoft("ms-refresh")
    body = route.calls[0].request.read().decode()
    assert "grant_type=refresh_token" in body
    assert "client_id=ms-client" in body
    assert "scope=https%3A%2F%2Fgraph.microsoft.com%2F.default+offline_access" in body
    assert "client_secret=ms-secret" in body
    assert tokens.access_token == "new-access-token"


@respx.mock
async def test_microsoft_authorization_code_exchange(microsoft_configured):
    route = respx.post(microsoft_token_url()).mock(
        return_value=httpx.Response(200, json=token_payload())
    )
    await exchange_microsoft_code("code", "verifier", "http://localhost:3000/callback")
    body = route.calls[0].request.read().decode()
    assert "grant_type=authorization_code" in body
    assert "code_verifier=verifier" in body


@respx.mock
async def test_invalid_grant_is_not_retryable(google_configured):
    respx.post(GOOGLE_TOKEN_URL).mock(
        return_value=httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
        )
    )
    with pytest.raises(OAuthError) as excinfo:
        await refresh_google("dead-refresh")
    assert excinfo.value.code == "invalid_grant"
    assert excinfo.value.retryable is False
    assert "revoked" in excinfo.value.description


@respx.mock
async def test_server_error_is_retryable(google_configured):
    respx.post(GOOGLE_TOKEN_URL).mock(return_value=httpx.Response(503, text="unavailable"))
    with pytest.raises(OAuthError) as excinfo:
        await refresh_google("refresh-token")
    assert excinfo.value.retryable is True


@respx.mock
async def test_unreachable_token_endpoint_is_retryable(google_configured):
    respx.post(GOOGLE_TOKEN_URL).mock(
        side_effect=httpx.ConnectError("refused", request=httpx.Request("POST", GOOGLE_TOKEN_URL))
    )
    with pytest.raises(OAuthError) as excinfo:
        await refresh_google("refresh-token")
    assert excinfo.value.code == "token_endpoint_unreachable"
    assert excinfo.value.retryable is True


@respx.mock
async def test_missing_access_token_is_rejected(google_configured):
    respx.post(GOOGLE_TOKEN_URL).mock(return_value=httpx.Response(200, json={"expires_in": 3600}))
    with pytest.raises(OAuthError) as excinfo:
        await refresh_google("refresh-token")
    assert excinfo.value.code == "invalid_token_response"
