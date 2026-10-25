"""SMTP transport hardening (spec section 15, item 3)."""

from __future__ import annotations

import ssl

from app.providers.smtp import build_tls_context


def test_tls_context_requires_verification_and_modern_tls():
    context = build_tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
