"""Operator authentication, session cookies, CSRF, and login throttling.

There is exactly one operator identity. Public registration does not exist.
Two authentication paths are supported:

- browser session: signed, HttpOnly cookie + double-submit CSRF token;
- automation: ``Authorization: Bearer <API_TOKEN>`` for local scripts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from fastapi import Request, Response

from app.config import settings
from app.core.errors import AuthenticationError, PermissionDenied, RateLimited

SESSION_COOKIE = "dlp_session"
CSRF_COOKIE = "dlp_csrf"
CSRF_HEADER = "x-csrf-token"

PBKDF2_PREFIX = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 600_000


# --------------------------------------------------------------------------
# Password hashing
# --------------------------------------------------------------------------


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    if len(password) < 10:
        raise ValueError("Password must be at least 10 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{PBKDF2_PREFIX}${iterations}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        prefix, iterations_raw, salt_raw, digest_raw = encoded.split("$", 3)
        if prefix != PBKDF2_PREFIX:
            return False
        iterations = int(iterations_raw)
        if iterations < 100_000 or iterations > 5_000_000:
            return False
        salt = _b64decode(salt_raw)
        expected = _b64decode(digest_raw)
    except (ValueError, TypeError):
        return False

    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


def verify_operator_password(password: str) -> bool:
    if settings.admin_password_hash:
        return verify_password(password, settings.admin_password_hash)
    if settings.admin_password:
        return hmac.compare_digest(password.encode("utf-8"), settings.admin_password.encode("utf-8"))
    return False


# --------------------------------------------------------------------------
# Session tokens (stateless HMAC; no server-side session table needed)
# --------------------------------------------------------------------------


def _sign(payload: bytes) -> str:
    digest = hmac.new(settings.session_secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return _b64encode(digest)


def issue_session_token(*, ttl_minutes: int | None = None, now: datetime | None = None) -> str:
    issued = now or datetime.now(timezone.utc)
    ttl = ttl_minutes if ttl_minutes is not None else settings.session_ttl_minutes
    payload = {
        "sub": "operator",
        "iat": int(issued.timestamp()),
        "exp": int(issued.timestamp()) + ttl * 60,
        "jti": secrets.token_hex(8),
    }
    encoded = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{encoded}.{_sign(encoded.encode('ascii'))}"


def verify_session_token(token: str) -> dict[str, object] | None:
    try:
        encoded, signature = token.split(".", 1)
    except ValueError:
        return None
    expected = _sign(encoded.encode("ascii"))
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        payload = json.loads(_b64decode(encoded))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("sub") != "operator":
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(datetime.now(timezone.utc).timestamp()):
        return None
    return payload


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_session_cookies(response: Response, *, token: str, csrf: str) -> None:
    secure = settings.public_base_url.startswith("https://")
    max_age = settings.session_ttl_minutes * 60
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    # Readable by the UI so it can echo it back in the X-CSRF-Token header.
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


# --------------------------------------------------------------------------
# Request principal
# --------------------------------------------------------------------------

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class Principal:
    actor: str
    method: Literal["session", "token"]
    request_id: str | None = None


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _check_origin(request: Request) -> None:
    """Reject cross-site browser writes (defence in depth on top of SameSite)."""

    origin = request.headers.get("origin")
    if origin is None:
        return
    if origin not in settings.allowed_origins:
        raise PermissionDenied("Origin is not allowed", code="origin_not_allowed")


def _check_csrf(request: Request) -> None:
    header = request.headers.get(CSRF_HEADER)
    cookie = request.cookies.get(CSRF_COOKIE)
    if not header or not cookie or not hmac.compare_digest(header, cookie):
        raise PermissionDenied("CSRF token missing or invalid", code="csrf_failed")


async def require_admin(request: Request) -> Principal:
    token = _bearer_token(request)
    if token is not None:
        if not settings.api_token or not hmac.compare_digest(token, settings.api_token):
            raise AuthenticationError("Invalid API token")
        return Principal(actor="api-token", method="token", request_id=_request_id(request))

    session_token = request.cookies.get(SESSION_COOKIE)
    payload = verify_session_token(session_token) if session_token else None
    if payload is None:
        raise AuthenticationError("Sign in required")

    if request.method in UNSAFE_METHODS:
        _check_origin(request)
        _check_csrf(request)

    return Principal(actor=f"operator:{payload.get('jti')}", method="session", request_id=_request_id(request))


async def optional_admin(request: Request) -> Principal | None:
    try:
        return await require_admin(request)
    except AuthenticationError:
        return None


# --------------------------------------------------------------------------
# Login throttling (single process; adequate for a private single-operator app)
# --------------------------------------------------------------------------


class LoginThrottle:
    def __init__(self, *, max_attempts: int, window_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        cutoff = now - self.window_seconds
        events = [t for t in self._events.get(key, []) if t >= cutoff]
        if events:
            self._events[key] = events
        else:
            self._events.pop(key, None)
        return events

    def assert_allowed(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            events = self._recent(key, now)
            if len(events) >= self.max_attempts:
                retry_after = max(1, int(self.window_seconds - (now - events[0])))
                raise RateLimited(
                    "Too many sign-in attempts. Try again later.",
                    details={"retry_after_seconds": retry_after},
                )

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._recent(key, now)
            self._events.setdefault(key, []).append(now)

    def record_success(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


login_throttle = LoginThrottle(
    max_attempts=settings.login_max_attempts,
    window_seconds=settings.login_window_seconds,
)


def client_key(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host


class SlidingWindowLimiter:
    """In-process limiter for local API writes (single-host deployment)."""

    def __init__(self, *, max_events: int, window_seconds: int) -> None:
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            events = [t for t in self._events.get(key, []) if t >= cutoff]
            if len(events) >= self.max_events:
                retry_after = max(1, int(self.window_seconds - (now - events[0])))
                raise RateLimited(
                    "Local API rate limit reached for this endpoint",
                    details={"retry_after_seconds": retry_after},
                )
            events.append(now)
            self._events[key] = events


send_limiter = SlidingWindowLimiter(
    max_events=settings.send_rate_limit_per_minute, window_seconds=60
)


async def enforce_send_rate_limit(request: Request) -> None:
    send_limiter.hit(client_key(request))
