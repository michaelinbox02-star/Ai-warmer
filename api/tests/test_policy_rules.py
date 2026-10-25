"""Unit tests for the pure policy rules (spec sections 6 and 7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from random import Random

import pytest

from app.core.policy_rules import (
    AuthPolicy,
    CircuitTrigger,
    circuit_after_failure,
    circuit_after_success,
    circuit_auto_close_allowed,
    decide_outcome,
    evaluate_readiness,
    retry_delay_seconds,
    suppression_blocks,
)

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


def readiness(**overrides):
    values = {
        "connection_valid": True,
        "transport": "gmail_api",
        "tls_valid": None,
        "sender_identity_verified": True,
        "auth_checks": {"spf": "pass", "dkim": "pass", "dmarc": "pass"},
        "auth_policy": AuthPolicy(),
        "global_sending_enabled": True,
        "account_mode": "production",
        "circuit_state": "closed",
        "credentials_valid": True,
        "acknowledged_warnings": (),
    }
    values.update(overrides)
    return evaluate_readiness(**values)


def checks_by_key(report):
    return {check.key: check for check in report.checks}


def test_fully_ready_account_has_no_blockers():
    report = readiness()
    assert report.ready is True
    assert report.blocking == ()
    assert report.warnings == ()


def test_connection_failure_blocks_and_cannot_be_acknowledged():
    report = readiness(connection_valid=False, acknowledged_warnings=("connection",))
    assert report.ready is False
    assert "connection" in report.blocking
    assert checks_by_key(report)["connection"].acknowledged is False


def test_required_auth_failure_blocks_even_when_acknowledged():
    report = readiness(
        auth_checks={"spf": "fail", "dkim": "pass", "dmarc": "pass"},
        acknowledged_warnings=("auth_spf",),
    )
    check = checks_by_key(report)["auth_spf"]
    assert check.status == "fail"
    assert check.blocking is True
    assert check.acknowledged is False
    assert "auth_spf" in report.blocking


def test_required_auth_warning_can_be_acknowledged():
    unacked = readiness(auth_checks={"spf": "warning", "dkim": "pass", "dmarc": "pass"})
    assert "auth_spf" in unacked.blocking

    acked = readiness(
        auth_checks={"spf": "warning", "dkim": "pass", "dmarc": "pass"},
        acknowledged_warnings=("auth_spf",),
    )
    assert acked.ready is True
    assert acked.blocking == ()
    assert checks_by_key(acked)["auth_spf"].acknowledged is True


def test_recommended_dmarc_warning_needs_acknowledgement():
    report = readiness(auth_checks={"spf": "pass", "dkim": "pass", "dmarc": "none"})
    assert report.ready is False
    check = checks_by_key(report)["auth_dmarc"]
    assert check.status == "warning"
    assert check.blocking is True

    acked = readiness(
        auth_checks={"spf": "pass", "dkim": "pass", "dmarc": "none"},
        acknowledged_warnings=("auth_dmarc",),
    )
    assert acked.ready is True
    assert checks_by_key(acked)["auth_dmarc"].acknowledged is True


def test_auth_policy_can_turn_checks_off():
    report = readiness(
        auth_checks={"spf": "fail", "dkim": "fail", "dmarc": "fail"},
        auth_policy=AuthPolicy(spf="off", dkim="off", dmarc="off"),
    )
    keys = set(checks_by_key(report))
    assert not {"auth_spf", "auth_dkim", "auth_dmarc"} & keys
    assert report.ready is True


def test_smtp_tls_must_be_validated():
    unknown = readiness(transport="smtp", tls_valid=None)
    assert "tls" in unknown.blocking
    assert checks_by_key(unknown)["tls"].status == "unknown"

    failed = readiness(transport="smtp", tls_valid=False)
    assert checks_by_key(failed)["tls"].status == "fail"

    passed = readiness(transport="smtp", tls_valid=True)
    assert passed.ready is True


def test_global_switch_and_mode_are_reported_but_acknowledgeable():
    paused = readiness(global_sending_enabled=False, acknowledged_warnings=("global_sending",))
    assert "global_sending" in paused.blocking
    assert checks_by_key(paused)["global_sending"].acknowledged is False

    test_only = readiness(account_mode="test_only")
    assert "account_mode" in test_only.blocking
    assert checks_by_key(test_only)["account_mode"].status == "warning"


def test_open_circuit_blocks_readiness():
    report = readiness(circuit_state="open")
    assert "circuit" in report.blocking
    assert checks_by_key(report)["circuit"].status == "fail"


@pytest.mark.parametrize("action", ["fail", "needs_review", "pause_account"])
def test_terminal_provider_outcomes_never_retry(action):
    kwargs = {
        "fail": {"retryability": "permanent"},
        "needs_review": {"retryability": "unknown"},
        "pause_account": {"retryability": "auth"},
    }[action]
    decision = decide_outcome(attempt_count=1, max_attempts=5, now=NOW, **kwargs)
    assert decision.action == action
    assert decision.retry_at is None


def test_transient_failure_retries_with_backoff():
    decision = decide_outcome(
        "transient",
        attempt_count=1,
        max_attempts=5,
        now=NOW,
        schedule=(100,),
        rng=Random(1),
    )
    assert decision.action == "retry"
    assert decision.retry_at is not None
    # 20% jitter: 80..120 seconds.
    delay = (decision.retry_at - NOW).total_seconds()
    assert 80 <= delay <= 120


def test_max_attempts_and_expiry_stop_retries():
    exhausted = decide_outcome("transient", attempt_count=3, max_attempts=3, now=NOW)
    assert exhausted.action == "fail"
    assert exhausted.reason == "max_attempts_exhausted"

    expired = decide_outcome(
        "transient",
        attempt_count=1,
        max_attempts=5,
        now=NOW,
        expires_at=NOW,
    )
    assert expired.action == "fail"
    assert expired.reason == "expired_before_retry"

    almost = decide_outcome(
        "transient",
        attempt_count=1,
        max_attempts=5,
        now=NOW,
        schedule=(600,),
        rng=Random(1),
        expires_at=NOW + timedelta(seconds=60),
    )
    assert almost.action == "fail"
    assert almost.reason == "expires_before_next_attempt"


def test_rate_limit_hint_is_honoured_and_capped():
    limited = decide_outcome(
        "rate_limited",
        attempt_count=1,
        max_attempts=5,
        now=NOW,
        schedule=(30,),
        retry_after_seconds=3600,
        rng=Random(1),
    )
    assert limited.action == "retry"
    delay = (limited.retry_at - NOW).total_seconds()
    assert 3600 <= delay <= 3600

    capped = decide_outcome(
        "rate_limited",
        attempt_count=1,
        max_attempts=5,
        now=NOW,
        schedule=(30,),
        retry_after_seconds=999_999,
        max_retry_after_seconds=600,
        rng=Random(1),
    )
    assert capped.retry_at is not None
    assert (capped.retry_at - NOW).total_seconds() <= 600


def test_retry_delay_is_bounded_and_clamped():
    assert retry_delay_seconds(1, schedule=(30,), jitter_ratio=0.0) == 30
    assert retry_delay_seconds(99, schedule=(30, 120), jitter_ratio=0.0) == 120
    assert retry_delay_seconds(1, schedule=(1,), jitter_ratio=0.9) >= 1

    with pytest.raises(ValueError):
        retry_delay_seconds(0)
    with pytest.raises(ValueError):
        retry_delay_seconds(1, schedule=())
    with pytest.raises(ValueError):
        retry_delay_seconds(1, jitter_ratio=1.0)


@pytest.mark.parametrize(
    "trigger",
    [
        CircuitTrigger.AUTH_FAILURE,
        CircuitTrigger.QUOTA_RESTRICTION,
        CircuitTrigger.TLS_FAILURE,
        CircuitTrigger.MANUAL,
        CircuitTrigger.COMPLAINT,
    ],
)
def test_enforcement_failures_require_manual_reset(trigger):
    decision = circuit_after_failure(trigger, now=NOW)
    assert decision is not None
    assert decision.state == "open"
    assert decision.requires_manual_reset is True
    assert decision.open_until is None


def test_transient_streak_opens_temporarily_after_threshold():
    below = circuit_after_failure(
        CircuitTrigger.TRANSIENT_STREAK, now=NOW, consecutive_transient=4
    )
    assert below is None

    decision = circuit_after_failure(
        CircuitTrigger.TRANSIENT_STREAK, now=NOW, consecutive_transient=5
    )
    assert decision is not None
    assert decision.requires_manual_reset is False
    assert decision.open_until == NOW + timedelta(minutes=30)
    assert circuit_auto_close_allowed(decision, now=NOW) is False
    assert circuit_auto_close_allowed(decision, now=NOW + timedelta(minutes=31)) is True


def test_forced_circuit_is_manual():
    decision = circuit_after_failure(CircuitTrigger.MANUAL, now=NOW, forced=True)
    assert decision is not None
    assert decision.requires_manual_reset is True


def test_success_closes_or_keeps_circuit():
    assert circuit_after_success("closed") is None
    reopened = circuit_after_success("open")
    assert reopened is not None
    assert reopened.state == "closed"


def test_suppression_rules():
    assert suppression_blocks("hard_bounce", removed_at=None)
    assert suppression_blocks("complaint", removed_at=None)
    assert not suppression_blocks("hard_bounce", removed_at=NOW)
    assert not suppression_blocks("transient", removed_at=None)
    assert not suppression_blocks(None, removed_at=None)
