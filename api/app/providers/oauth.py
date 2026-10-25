"""OAuth token exchange helpers (Google and Microsoft)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import settings
from app.core.redaction import sanitize_text

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GRAPH_SCOPE = "https://graph.microsoft.com/.default offline_access"


class OAuthError(Exception):
    def __init__(self, code: str, description: str, *, retryable: bool = False) -> None:
        super().__init__(description)
        self.code = code
        self.description = sanitize_text(description, limit=300) or "OAuth error"
        self.retryable = retryable


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    expires_at: datetime
    token_type: str = "Bearer"
    refresh_token: str | None = None
    scopes: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    def as_secret_update(self, previous: dict[str, Any]) -> dict[str, Any]:
        updated = dict(previous)
        updated["access_token"] = self.access_token
        updated["expires_at"] = self.expires_at.isoformat()
        updated["token_type"] = self.token_type
        if self.refresh_token:
            updated["refresh_token"] = self.refresh_token
        if self.scopes:
            updated["scopes"] = list(self.scopes)
        return updated


def _expires_at(payload: dict[str, Any]) -> datetime:
    try:
        seconds = int(payload.get("expires_in", 3600))
    except (TypeError, ValueError):
        seconds = 3600
    return datetime.now(timezone.utc) + timedelta(seconds=max(60, seconds))


async def _post_token(url: str, data: dict[str, str]) -> TokenSet:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, data=data, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise OAuthError("token_endpoint_unreachable", str(exc), retryable=True) from exc

    if response.status_code >= 400:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        code = str(payload.get("error", f"http_{response.status_code}"))
        description = str(payload.get("error_description") or payload.get("error") or "Token exchange failed")
        retryable = response.status_code >= 500 or code in {"temporarily_unavailable", "server_error"}
        raise OAuthError(code, description, retryable=retryable)

    try:
        payload = response.json()
    except ValueError as exc:
        raise OAuthError("invalid_token_response", "Token endpoint returned invalid JSON") from exc

    access_token = payload.get("access_token")
    if not access_token:
        raise OAuthError("invalid_token_response", "Token endpoint did not return an access token")

    scopes_raw = payload.get("scope") or ""
    scopes = tuple(sorted({s for s in str(scopes_raw).split() if s}))
    return TokenSet(
        access_token=str(access_token),
        expires_at=_expires_at(payload),
        token_type=str(payload.get("token_type", "Bearer")),
        refresh_token=payload.get("refresh_token"),
        scopes=scopes,
        raw=payload,
    )


async def refresh_google(refresh_token: str) -> TokenSet:
    if not settings.google_client_id or not settings.google_client_secret:
        raise OAuthError(
            "google_oauth_not_configured",
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are not configured",
        )
    return await _post_token(
        GOOGLE_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
        },
    )


async def exchange_google_code(code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
    if not settings.google_client_id or not settings.google_client_secret:
        raise OAuthError("google_oauth_not_configured", "Google OAuth is not configured")
    return await _post_token(
        GOOGLE_TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
        },
    )


def microsoft_token_url() -> str:
    return f"https://login.microsoftonline.com/{settings.microsoft_tenant}/oauth2/v2.0/token"


async def refresh_microsoft(refresh_token: str) -> TokenSet:
    if not settings.microsoft_client_id:
        raise OAuthError(
            "microsoft_oauth_not_configured",
            "MICROSOFT_CLIENT_ID is not configured",
        )
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": settings.microsoft_client_id,
        "scope": GRAPH_SCOPE,
    }
    if settings.microsoft_client_secret:
        data["client_secret"] = settings.microsoft_client_secret
    return await _post_token(microsoft_token_url(), data)


async def exchange_microsoft_code(code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
    if not settings.microsoft_client_id:
        raise OAuthError("microsoft_oauth_not_configured", "Microsoft OAuth is not configured")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "client_id": settings.microsoft_client_id,
        "scope": GRAPH_SCOPE,
    }
    if settings.microsoft_client_secret:
        data["client_secret"] = settings.microsoft_client_secret
    return await _post_token(microsoft_token_url(), data)


def token_is_fresh(secret: dict[str, Any], *, skew_seconds: int = 90) -> bool:
    raw = secret.get("expires_at")
    if not raw:
        return False
    try:
        expires_at = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at - timedelta(seconds=skew_seconds) > datetime.now(timezone.utc)
