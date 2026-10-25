"""Redaction and masking helpers (spec section 10)."""

from __future__ import annotations

from app.core.redaction import mask_email, sanitize_text


def test_mask_email_keeps_only_domain_and_initial():
    assert mask_email("alice@example.com") == "a***@example.com"
    assert mask_email("a@example.com") == "***@example.com"
    assert mask_email("not-an-email") == "[redacted]"
    assert "alice" not in mask_email("alice@example.com")


def test_bearer_tokens_are_redacted():
    cleaned = sanitize_text("Authorization: Bearer ya29.a0AfH6SMBexample-token-value")
    assert "ya29" not in cleaned
    assert "REDACTED" in cleaned


def test_password_secret_and_api_keys_are_redacted():
    for text in (
        "password=hunter2",
        "client_secret: abc123",
        "api_key=sk-live-123456789",
        "token: opaque-value",
    ):
        cleaned = sanitize_text(text)
        assert "hunter2" not in cleaned
        assert "abc123" not in cleaned
        assert "sk-live" not in cleaned
        assert "opaque-value" not in cleaned
        assert "[REDACTED]" in cleaned


def test_jwts_and_long_blobs_are_redacted():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJvcGVyYXRvciJ9.c2lnbmF0dXJl"
    assert sanitize_text(jwt) == "[REDACTED-JWT]"
    # Long hex values also satisfy the broader base64-blob pattern; either
    # marker is acceptable as long as the original value is gone.
    assert sanitize_text("a" * 64) in {"[REDACTED-BLOB]", "[REDACTED-HEX]"}


def test_credentials_in_urls_are_redacted():
    cleaned = sanitize_text("connecting to https://user:pa55word@example.com/path")
    assert "pa55word" not in cleaned
    assert "[REDACTED]@" in cleaned


def test_control_characters_and_newlines_are_flattened():
    cleaned = sanitize_text("first\r\nsecond\x00third\x07")
    assert cleaned == "first second third"


def test_sanitize_text_bounds_length():
    cleaned = sanitize_text("word " * 200, limit=50)
    assert cleaned is not None
    assert len(cleaned) == 50
    assert cleaned.endswith("…")


def test_sanitize_text_handles_none():
    assert sanitize_text(None) is None
