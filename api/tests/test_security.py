"""Session security, password hashing, and local throttles."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from app.core.errors import RateLimited
from app.security import (
    LoginThrottle,
    SlidingWindowLimiter,
    hash_password,
    issue_session_token,
    verify_password,
    verify_session_token,
)


def test_password_hash_roundtrip_and_rejection():
    encoded = hash_password("correct horse battery", iterations=100_000)
    assert encoded.startswith("pbkdf2_sha256$100000$")
    assert verify_password("correct horse battery", encoded)
    assert not verify_password("wrong password", encoded)
    assert not verify_password("correct horse battery", "bogus")


def test_password_hash_requires_minimum_length():
    with pytest.raises(ValueError):
        hash_password("short")


def test_password_hash_rejects_out_of_range_iterations():
    encoded = "pbkdf2_sha256$1000$c2FsdA$aGFzaA"
    assert not verify_password("anything", encoded)


def test_session_token_roundtrip():
    token = issue_session_token(ttl_minutes=5)
    payload = verify_session_token(token)
    assert payload is not None
    assert payload["sub"] == "operator"
    assert isinstance(payload["jti"], str)
    assert payload["exp"] > payload["iat"]


def test_tampered_session_token_is_rejected():
    token = issue_session_token(ttl_minutes=5)
    encoded, signature = token.split(".", 1)
    assert verify_session_token(f"{encoded}.{signature[:-1]}x") is None
    assert verify_session_token(f"{encoded}x.{signature}") is None
    assert verify_session_token("garbage") is None
    assert verify_session_token("") is None


def test_expired_session_token_is_rejected():
    issued = datetime.now(timezone.utc) - timedelta(hours=2)
    token = issue_session_token(ttl_minutes=1, now=issued)
    assert verify_session_token(token) is None


def test_login_throttle_blocks_and_resets():
    throttle = LoginThrottle(max_attempts=2, window_seconds=60)
    throttle.assert_allowed("1.2.3.4")
    throttle.record_failure("1.2.3.4")
    throttle.assert_allowed("1.2.3.4")
    throttle.record_failure("1.2.3.4")
    with pytest.raises(RateLimited):
        throttle.assert_allowed("1.2.3.4")

    # A different client is unaffected.
    throttle.assert_allowed("5.6.7.8")

    throttle.record_success("1.2.3.4")
    throttle.assert_allowed("1.2.3.4")


def test_login_throttle_window_expires():
    throttle = LoginThrottle(max_attempts=1, window_seconds=1)
    throttle.record_failure("client")
    with pytest.raises(RateLimited):
        throttle.assert_allowed("client")
    time.sleep(1.05)
    throttle.assert_allowed("client")


def test_sliding_window_limiter():
    limiter = SlidingWindowLimiter(max_events=2, window_seconds=60)
    limiter.hit("ip")
    limiter.hit("ip")
    with pytest.raises(RateLimited):
        limiter.hit("ip")
    limiter.hit("other-ip")
