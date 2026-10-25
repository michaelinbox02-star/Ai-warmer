"""Provider HTTP error classification and SMTP result mapping."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.providers.http_common import (
    classify_http_error,
    classify_transport_error,
    error_body,
    parse_retry_after,
)
from app.providers.smtp import SMTPProvider, build_tls_context


def response(status: int, *, json_body=None, headers=None, text: str = "") -> httpx.Response:
    request = httpx.Request("POST", "https://example.test/send")
    if json_body is not None:
        return httpx.Response(status, json=json_body, headers=headers, request=request)
    return httpx.Response(status, text=text, headers=headers, request=request)


def test_quota_and_rate_limit_classification():
    classified = classify_http_error(response(429, headers={"retry-after": "120"}))
    assert classified.retryability == "rate_limited"
    assert classified.circuit_trigger == "quota_restriction"
    assert classified.retry_after_seconds == 120

    by_code = classify_http_error(response(400), provider_code="QuotaExceeded")
    assert by_code.retryability == "rate_limited"

    capacity = classify_http_error(response(507))
    assert capacity.retryability == "rate_limited"


def test_auth_classification():
    assert classify_http_error(response(401)).retryability == "auth"
    assert classify_http_error(response(403)).retryability == "auth"
    by_code = classify_http_error(response(400), provider_code="invalid_grant")
    assert by_code.retryability == "auth"
    assert by_code.circuit_trigger == "auth_failure"


def test_permanent_ambiguous_and_transient_classification():
    assert classify_http_error(response(400)).retryability == "permanent"
    assert classify_http_error(response(422)).retryability == "permanent"
    assert classify_http_error(response(413)).retryability == "permanent"

    # The provider may have accepted the request before timing out.
    assert classify_http_error(response(408)).retryability == "unknown"
    assert classify_http_error(response(504)).retryability == "unknown"

    transient = classify_http_error(response(503))
    assert transient.retryability == "transient"
    assert transient.diagnostic_code == "http_503"


def test_transport_error_classification():
    connect = classify_transport_error(
        httpx.ConnectError("refused", request=httpx.Request("POST", "https://x.test"))
    )
    assert connect.retryability == "transient"

    read_timeout = classify_transport_error(
        httpx.ReadTimeout("timeout", request=httpx.Request("POST", "https://x.test"))
    )
    assert read_timeout.retryability == "unknown"

    protocol = classify_transport_error(
        httpx.RemoteProtocolError("dropped", request=httpx.Request("POST", "https://x.test"))
    )
    assert protocol.retryability == "unknown"


def test_retry_after_parsing():
    assert parse_retry_after("60") == 60
    assert parse_retry_after(None) is None
    assert parse_retry_after("not-a-date") is None

    future = datetime.now(timezone.utc) + timedelta(seconds=90)
    parsed = parse_retry_after(future.strftime("%a, %d %b %Y %H:%M:%S GMT"))
    assert parsed is not None and 80 <= parsed <= 91

    past = datetime.now(timezone.utc) - timedelta(seconds=90)
    assert parse_retry_after(past.strftime("%a, %d %b %Y %H:%M:%S GMT")) == 0


def test_error_body_shapes_are_sanitized():
    gmail = response(
        400,
        json_body={
            "error": {
                "code": 400,
                "message": "Invalid grant: password=hunter2",
                "status": "INVALID_ARGUMENT",
            }
        },
    )
    code, message = error_body(gmail)
    assert code == "400"
    assert "hunter2" not in message
    assert "[REDACTED]" in message

    graph = response(401, json_body={"error": "invalid_grant", "error_description": "expired"})
    assert error_body(graph) == ("invalid_grant", "expired")

    plain = response(502, text="upstream exploded")
    assert error_body(plain) == ("", "upstream exploded")


def test_smtp_tls_context_is_strict():
    import ssl

    context = build_tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


@pytest.mark.parametrize(
    "code,expected",
    [
        (535, "auth"),
        (530, "auth"),
        (421, "transient"),
        (450, "transient"),
        (550, "permanent"),
        (554, "permanent"),
        (250, "transient"),
        (None, "transient"),
    ],
)
def test_smtp_code_classification(code, expected):
    assert SMTPProvider._classify_code(code, "text") == expected


def test_smtp_enhanced_code_extraction():
    assert SMTPProvider._enhanced("5.1.1 user unknown") == "5.1.1"
    assert SMTPProvider._enhanced(b"421 4.7.0 try later") == "4.7.0"
    assert SMTPProvider._enhanced("no code here") is None
    assert SMTPProvider._enhanced(None) is None


def test_smtp_circuit_triggers():
    provider = SMTPProvider()
    assert provider._circuit_for("auth", "auth failed") == "auth_failure"
    assert provider._circuit_for("transient", "quota exceeded") == "quota_restriction"
    assert provider._circuit_for("permanent", "rate limit reached") == "quota_restriction"
    assert provider._circuit_for("transient", "temporary failure") is None
