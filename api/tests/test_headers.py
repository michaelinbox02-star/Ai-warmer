"""Tests for received-header parsing used by controlled tests."""

from __future__ import annotations

from app.core.headers import (
    header_slice,
    parse_authentication_results,
    relaxed_align,
    strict_align,
)


GMAIL_HEADER = (
    "mx.google.com; dkim=pass header.i=@example.com header.s=google header.b=abc; "
    "spf=pass (google.com: domain of bounce@example.com designates 203.0.113.5 as permitted sender) "
    "smtp.mailfrom=bounce@example.com; "
    "dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=example.com"
)


def test_parse_typical_authentication_results():
    summary = parse_authentication_results(GMAIL_HEADER)
    assert summary.from_domain == "example.com"
    assert summary.spf_domain == "example.com"
    assert summary.dkim_domain == "example.com"
    assert summary.dmarc_domain == "example.com"
    assert summary.mechanisms["spf"].result == "pass"
    assert summary.mechanisms["dkim"].result == "pass"
    assert summary.mechanisms["dmarc"].result == "pass"
    assert summary.mechanisms["dkim"].properties["header.s"] == "google"
    assert summary.alignment == {
        "spf_aligned": "pass",
        "dkim_aligned": "pass",
        "dmarc_aligned": "pass",
    }


def test_parse_handles_folded_headers_and_comments():
    raw = (
        "mx.example.net;\r\n"
        "\tdkim=pass header.d=mail.example.com header.s=selector1;\r\n"
        "\tspf=pass smtp.mailfrom=mail.example.com;\r\n"
        "\tdmarc=pass header.from=example.com"
    )
    summary = parse_authentication_results(raw)
    assert summary.from_domain == "example.com"
    assert summary.dkim_domain == "mail.example.com"
    assert summary.mechanisms["dkim"].result == "pass"
    assert "\r" not in summary.raw


def test_last_occurrence_wins_and_unknown_results_are_normalized():
    raw = (
        "auth.example; spf=fail smtp.mailfrom=bad.example; "
        "spf=pass smtp.mailfrom=good.example; dkim=weird header.d=example.com"
    )
    summary = parse_authentication_results(raw)
    assert summary.mechanisms["spf"].result == "pass"
    assert summary.spf_domain == "good.example"
    assert summary.mechanisms["dkim"].result == "unknown"


def test_failed_mechanisms_are_reported_separately_from_alignment():
    raw = (
        "mx.example; spf=pass smtp.mailfrom=other.example; "
        "dkim=pass header.d=other.example; dmarc=fail header.from=example.com"
    )
    summary = parse_authentication_results(raw)
    # Alignment compares identifiers; the mechanism verdicts are reported
    # separately and are what the readiness gate consumes.
    assert summary.alignment["spf_aligned"] == "fail"
    assert summary.alignment["dkim_aligned"] == "fail"
    assert summary.mechanisms["dmarc"].result == "fail"
    assert summary.dmarc_domain == "example.com"


def test_relaxed_and_strict_alignment():
    assert relaxed_align("mail.example.com", "example.com")
    assert relaxed_align("example.com", "mail.example.com")
    assert relaxed_align("EXAMPLE.com.", "example.COM")
    assert not relaxed_align("evil-example.com", "example.com")
    assert strict_align("example.com.", "EXAMPLE.COM")
    assert not strict_align("mail.example.com", "example.com")
    assert not strict_align(None, "example.com")


def test_header_slice_extracts_all_authentication_results():
    raw = (
        "Received: from mx.example by inbox.example;\r\n"
        "Authentication-Results: mx.example; spf=pass smtp.mailfrom=example.com;\r\n"
        "\tdkim=pass header.d=example.com\r\n"
        "Subject: hello\r\n"
        "Authentication-Results: relay.example; dmarc=pass header.from=example.com"
    )
    sliced = header_slice(raw)
    assert sliced is not None
    assert sliced.count("Authentication-Results") == 0
    assert "spf=pass" in sliced
    assert "dmarc=pass" in sliced
    assert sliced.count("\n") == 1


def test_header_slice_returns_none_when_absent():
    assert header_slice("Subject: hello\nFrom: a@example.com") is None
    assert header_slice("") is None


def test_authentication_summary_dict_contains_no_credentials():
    summary = parse_authentication_results(GMAIL_HEADER).as_dict()
    serialized = repr(summary)
    assert "password" not in serialized.lower()
    assert "bearer" not in serialized.lower()
    assert "raw" not in summary
