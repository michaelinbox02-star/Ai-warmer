"""Redaction helpers for logs, error detail, and AI prompts."""

from __future__ import annotations

import re

_DEFAULT_LIMIT = 500

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]+"), "[REDACTED-TOKEN]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"), "[REDACTED-JWT]"),
    (
        re.compile(r"(?i)\b(password|passwd|secret|token|api[-_]?key|client[-_]?secret)\b\s*[:=]\s*\S+"),
        r"\1=[REDACTED]",
    ),
    (re.compile(r"\bhttps?://[^\s/@:]+:[^\s/@]+@"), "https://[REDACTED]@"),
    (re.compile(r"\b[A-Za-z0-9+/]{48,}={0,2}\b"), "[REDACTED-BLOB]"),
    (re.compile(r"\b[A-Fa-f0-9]{48,}\b"), "[REDACTED-HEX]"),
)

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def mask_email(address: str) -> str:
    value = address.strip()
    if "@" not in value:
        return "[redacted]"
    local, _, domain = value.rpartition("@")
    if not local:
        return f"***@{domain}"
    keep = local[0] if len(local) > 1 else ""
    return f"{keep}***@{domain}"


def sanitize_text(value: str | None, *, limit: int = _DEFAULT_LIMIT) -> str | None:
    """Return a single-line, secret-free, length-bounded copy of `value`."""

    if value is None:
        return None
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub(" ", text)
    text = " ".join(text.split())
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text
