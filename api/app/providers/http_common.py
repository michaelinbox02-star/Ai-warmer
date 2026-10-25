"""Shared HTTP error classification for provider REST APIs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

import httpx

from app.core.policy_rules import Retryability
from app.core.redaction import sanitize_text

_QUOTA_CODES = frozenset(
    {
        "ratelimitexceeded",
        "userratelimitexceeded",
        "quotaexceeded",
        "resourcenotavailable",
        "toomanyrequests",
        "throttled",
        "activitylimitreached",
        "mailboxconcurrency",
        "applicationthrottled",
    }
)
_AUTH_CODES = frozenset(
    {
        "invalidauthentication",
        "insufficientpermissions",
        "authorization_requestdenied",
        "unauthenticated",
        "invalid_grant",
        "invalid_token",
        "expired_token",
        "accessdenied",
        "authenticationfailure",
    }
)


@dataclass(frozen=True)
class HttpError:
    retryability: Retryability
    diagnostic_code: str
    detail: str
    retry_after_seconds: int | None = None
    circuit_trigger: str | None = None


def parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (when - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(delta))


def error_body(response: httpx.Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except ValueError:
        return "", sanitize_text(response.text, limit=300) or ""

    if not isinstance(payload, Mapping):
        return "", ""

    if isinstance(payload.get("error"), Mapping):
        error = payload["error"]
        code = str(error.get("code") or error.get("status") or "")
        message = str(error.get("message") or "")
        return code, sanitize_text(message, limit=300) or ""

    code = str(payload.get("error") or "")
    message = str(payload.get("error_description") or payload.get("message") or "")
    return code, sanitize_text(message, limit=300) or ""


def classify_http_error(
    response: httpx.Response,
    *,
    provider_code: str = "",
    message: str = "",
) -> HttpError:
    status = response.status_code
    code_lower = provider_code.lower()
    retry_after = parse_retry_after(response.headers.get("retry-after"))
    detail = message or sanitize_text(response.reason_phrase, limit=120) or f"HTTP {status}"

    if code_lower in _QUOTA_CODES or status in (429, 507):
        return HttpError(
            "rate_limited",
            provider_code or f"http_{status}_rate_limited",
            detail,
            retry_after,
            "quota_restriction",
        )
    if status == 401 or code_lower in _AUTH_CODES:
        return HttpError("auth", provider_code or "http_401_unauthorized", detail, retry_after, "auth_failure")
    if status == 403:
        # A 403 without a quota/auth code is still an authorization problem:
        # do not hammer the provider.
        return HttpError("auth", provider_code or "http_403_forbidden", detail, retry_after, "auth_failure")
    if status in (408, 504):
        return HttpError("unknown", provider_code or f"http_{status}_ambiguous", detail, retry_after)
    if status in (400, 404, 405, 409, 413, 415, 422):
        return HttpError("permanent", provider_code or f"http_{status}", detail, retry_after)
    if 500 <= status < 600:
        return HttpError("transient", provider_code or f"http_{status}", detail, retry_after)
    return HttpError("transient", provider_code or f"http_{status}", detail, retry_after)


def classify_transport_error(exc: httpx.HTTPError) -> HttpError:
    """Map transport failures.

    Connect-phase errors are safe to retry; read/write/protocol errors after
    the request was sent are ambiguous and must go to review.
    """

    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return HttpError("transient", "provider_connect_failed", sanitize_text(str(exc), limit=200) or "")
    if isinstance(exc, httpx.ReadTimeout):
        return HttpError("unknown", "provider_read_timeout", "Provider response timed out after submission")
    if isinstance(exc, (httpx.WriteTimeout, httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)):
        return HttpError("unknown", "provider_connection_interrupted", "Connection dropped during submission")
    return HttpError("transient", "provider_transport_error", sanitize_text(str(exc), limit=200) or "")


def base_http_metadata(response: httpx.Response, request_id: str | None = None) -> dict[str, Any]:
    return {
        "http_status": response.status_code,
        "provider_request_id": request_id
        or response.headers.get("x-request-id")
        or response.headers.get("request-id"),
    }
