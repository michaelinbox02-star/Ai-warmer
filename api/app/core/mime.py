"""RFC-compliant message construction.

No template engine and no arbitrary code: callers supply text/HTML bodies and
we assemble a bounded multipart message.
"""

from __future__ import annotations

import re
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from datetime import datetime, timezone
from typing import Iterable

from app.core.errors import ValidationError
from app.providers.base import OutboundMessage

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = _TAG_RE.sub(" ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def build_email_message(
    message: OutboundMessage,
    *,
    extra_headers: Iterable[tuple[str, str]] = (),
) -> EmailMessage:
    if not message.to_email or "@" not in message.to_email:
        raise ValidationError("Recipient address is invalid")
    if not message.subject.strip():
        raise ValidationError("Subject must not be empty")

    mime = EmailMessage()
    mime["To"] = message.to_email
    mime["From"] = (
        f"{message.from_name} <{message.from_email}>" if message.from_name else message.from_email
    )
    if message.reply_to_email:
        mime["Reply-To"] = message.reply_to_email
    mime["Subject"] = message.subject
    mime["Date"] = format_datetime(message.created_at or datetime.now(timezone.utc))
    mime["Message-ID"] = make_msgid(idstring=str(message.message_id), domain=_domain_of(message.from_email))
    mime["MIME-Version"] = "1.0"
    mime["Auto-Submitted"] = "auto-generated" if message.kind == "transactional" else "auto-replied"

    for key, value in message.headers.items():
        if key.lower() in {"to", "from", "subject", "date", "message-id", "mime-version"}:
            continue
        mime[key] = value
    for key, value in extra_headers:
        mime[key] = value

    text_body = message.text_body
    if not text_body and message.html_body:
        text_body = html_to_text(message.html_body)

    if message.html_body:
        mime.set_content(text_body or "")
        mime.add_alternative(message.html_body, subtype="html")
    else:
        mime.set_content(text_body or "")

    return mime


def build_message(
    message: OutboundMessage,
    *,
    extra_headers: Iterable[tuple[str, str]] = (),
    max_bytes: int = 1024 * 1024,
) -> bytes:
    raw = build_email_message(message, extra_headers=extra_headers).as_bytes()
    if len(raw) > max_bytes:
        raise ValidationError(f"Message exceeds the {max_bytes} byte limit")
    return raw


def build_raw_for_api(message: OutboundMessage, *, max_bytes: int = 1024 * 1024) -> bytes:
    """Same message, with transport-specific headers left to the adapter."""

    return build_message(message, max_bytes=max_bytes)


def _domain_of(address: str) -> str:
    _, _, domain = address.rpartition("@")
    return domain.strip().lower() or "localhost"
