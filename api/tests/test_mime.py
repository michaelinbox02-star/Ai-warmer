"""MIME construction and header-injection resistance."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.core.errors import ValidationError
from app.core.mime import build_email_message, build_message, html_to_text
from app.providers.base import OutboundMessage

MESSAGE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def message(**overrides):
    values = {
        "message_id": MESSAGE_ID,
        "from_email": "sender@example.com",
        "to_email": "customer@example.com",
        "subject": "Your receipt",
        "text_body": "Thank you.",
        "html_body": None,
        "from_name": "Example Service",
        "reply_to_email": "support@example.com",
        "headers": {},
        "kind": "transactional",
        "created_at": datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return OutboundMessage(**values)


def test_basic_headers_and_body():
    mime = build_email_message(message())
    assert mime["To"] == "customer@example.com"
    assert mime["From"] == "Example Service <sender@example.com>"
    assert mime["Reply-To"] == "support@example.com"
    assert mime["Subject"] == "Your receipt"
    assert mime["Auto-Submitted"] == "auto-generated"
    assert mime["Message-ID"].endswith("@example.com>")
    assert mime["MIME-Version"] == "1.0"
    assert "Thank you." in mime.get_content()


def test_diagnostic_test_is_marked_auto_replied():
    mime = build_email_message(message(kind="diagnostic_test"))
    assert mime["Auto-Submitted"] == "auto-replied"


def test_html_alternative_and_text_fallback():
    mime = build_email_message(
        message(text_body=None, html_body="<p>Hello <strong>world</strong></p><br>Bye")
    )
    assert mime.is_multipart()
    plain = mime.get_body(("plain",))
    html = mime.get_body(("html",))
    assert plain is not None and "Hello world" in plain.get_content()
    assert html is not None and "<strong>world</strong>" in html.get_content()


def test_html_to_text_strips_scripts_styles_and_tags():
    text = html_to_text(
        "<style>p{color:red}</style><p>Hello&nbsp;<b>there</b></p><br><script>alert(1)</script>Bye"
    )
    assert "color" not in text
    assert "alert" not in text
    assert "Hello there" in text
    assert text.endswith("Bye")


def test_reserved_headers_cannot_be_overridden_from_message_headers():
    mime = build_email_message(
        message(headers={"To": "attacker@evil.test", "Subject": "Injected", "X-Custom": "ok"})
    )
    assert mime["To"] == "customer@example.com"
    assert mime["Subject"] == "Your receipt"
    assert mime["X-Custom"] == "ok"


def test_diagnostic_header_is_added_by_transport():
    mime = build_email_message(
        message(kind="diagnostic_test"),
        extra_headers=(("X-Deliverability-Test", "controlled"),),
    )
    assert mime["X-Deliverability-Test"] == "controlled"


def test_invalid_recipient_and_empty_subject_are_rejected():
    with pytest.raises(ValidationError):
        build_email_message(message(to_email="not-an-address"))
    with pytest.raises(ValidationError):
        build_email_message(message(subject="   "))


def test_size_limit_is_enforced():
    with pytest.raises(ValidationError):
        build_message(message(text_body="x" * 5000), max_bytes=1024)


def test_header_injection_via_subject_is_rejected_by_the_stdlib():
    with pytest.raises(ValueError):
        build_email_message(message(subject="Hello\r\nBcc: victim@example.com"))
