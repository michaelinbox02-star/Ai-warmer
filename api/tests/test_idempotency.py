"""Idempotency key and fingerprint behaviour (spec section 5)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.idempotency import (
    canonical_request,
    fingerprint_matches,
    request_fingerprint,
    validate_idempotency_key,
)

ACCOUNT = uuid.UUID("11111111-1111-1111-1111-111111111111")
EXPIRES = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def payload(**overrides):
    values = {
        "account_id": ACCOUNT,
        "to_email": "Customer@Example.com",
        "from_email": "sender@example.com",
        "reply_to_email": None,
        "subject": "Receipt",
        "text_body": "Line one\r\nLine two",
        "html_body": None,
        "template_key": None,
        "template_version": None,
        "expires_at": EXPIRES,
        "kind": "transactional",
    }
    values.update(overrides)
    return values


def test_canonical_form_normalizes_equivalent_inputs():
    first = canonical_request(payload())
    second = canonical_request(
        payload(
            to_email="  CUSTOMER@example.COM ",
            text_body="Line one\nLine two",
        )
    )
    assert first == second
    assert first["to"] == "customer@example.com"
    assert first["text_body"] == "Line one\nLine two"


def test_fingerprint_is_stable_and_content_sensitive():
    base = request_fingerprint(payload())
    assert base == request_fingerprint(payload())
    assert base != request_fingerprint(payload(subject="Receipt 2"))
    assert base != request_fingerprint(payload(text_body="Different"))
    assert base != request_fingerprint(payload(expires_at=EXPIRES + timedelta(hours=1)))
    assert base != request_fingerprint(payload(kind="diagnostic_test"))


def test_fingerprint_matches_requires_exact_stored_value():
    fingerprint = request_fingerprint(payload())
    assert fingerprint_matches(fingerprint, fingerprint)
    assert not fingerprint_matches(fingerprint, fingerprint[:-1] + "0")
    assert not fingerprint_matches(None, fingerprint)
    assert not fingerprint_matches("", fingerprint)


def test_validate_idempotency_key():
    assert validate_idempotency_key(None) is None
    assert validate_idempotency_key("   ") is None
    assert validate_idempotency_key("  abc-123  ") == "abc-123"
    assert validate_idempotency_key("x" * 255) == "x" * 255

    with pytest.raises(ValueError):
        validate_idempotency_key("x" * 256)
    with pytest.raises(ValueError):
        validate_idempotency_key("bad\nkey")
    with pytest.raises(ValueError):
        validate_idempotency_key("bad\x00key")
