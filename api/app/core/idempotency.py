"""Idempotency fingerprints.

A caller-supplied idempotency key is scoped to an account. Replaying the same
key with a semantically identical payload returns the stored message; replaying
it with a different payload is a conflict (spec section 5).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping


def _normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return text


def _normalize_email(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip().lower() or None


def canonical_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stable representation of a send request.

    Deliberately excludes transport-only fields (headers, request ids) and
    includes everything that changes what the recipient receives.
    """

    return {
        "account_id": str(payload["account_id"]).lower(),
        "to": _normalize_email(payload.get("to_email")),
        "from": _normalize_email(payload.get("from_email")),
        "reply_to": _normalize_email(payload.get("reply_to_email")),
        "subject": _normalize_text(payload.get("subject")) or "",
        "text_body": _normalize_text(payload.get("text_body")),
        "html_body": _normalize_text(payload.get("html_body")),
        "template_key": payload.get("template_key"),
        "template_version": payload.get("template_version"),
        "expires_at": (
            payload["expires_at"].isoformat() if payload.get("expires_at") is not None else None
        ),
        "kind": payload.get("kind", "transactional"),
    }


def request_fingerprint(payload: Mapping[str, Any]) -> str:
    canonical = canonical_request(payload)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def fingerprint_matches(stored: str | None, candidate: str) -> bool:
    if not stored:
        return False
    return hmac.compare_digest(stored, candidate)


def validate_idempotency_key(value: str | None) -> str | None:
    """Idempotency keys are opaque but bounded, to prevent abuse and bloat."""

    if value is None:
        return None
    key = value.strip()
    if not key:
        return None
    if len(key) > 255:
        raise ValueError("Idempotency-Key must be <= 255 characters")
    if any(ord(ch) < 0x20 for ch in key):
        raise ValueError("Idempotency-Key must not contain control characters")
    return key
