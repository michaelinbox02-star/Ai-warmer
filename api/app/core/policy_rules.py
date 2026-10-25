"""Pure policy rules.

Nothing in this module touches the database, the network, or the clock beyond
values passed in. That keeps the safety-critical decision logic exhaustively
unit-testable (see `tests/test_policy_rules.py`).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable, Literal, Mapping, Sequence

CheckStatus = Literal["pass", "warning", "fail", "unknown", "not_applicable"]
Requirement = Literal["required", "recommended", "off"]
Transport = Literal["gmail_api", "microsoft_graph", "smtp"]
AccountMode = Literal["test_only", "production", "paused"]
CircuitState = Literal["closed", "open", "half_open"]
Retryability = Literal["transient", "permanent", "auth", "rate_limited", "unknown"]
Action = Literal["retry", "fail", "needs_review", "pause_account"]


# --------------------------------------------------------------------------
# Readiness gate (spec section 6)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthPolicy:
    """Per-domain authentication requirements.

    Defaults follow the spec: SPF and DKIM are required, DMARC is strongly
    recommended, MX is informational only.
    """

    spf: Requirement = "required"
    dkim: Requirement = "required"
    dmarc: Requirement = "recommended"
    mx: Requirement = "off"

    def requirement_for(self, check_type: str) -> Requirement:
        return getattr(self, check_type, "off")  # type: ignore[arg-type]


@dataclass(frozen=True)
class ReadinessCheck:
    key: str
    label: str
    status: CheckStatus
    detail: str
    blocking: bool = False
    acknowledged: bool = False


@dataclass(frozen=True)
class ReadinessReport:
    checks: tuple[ReadinessCheck, ...]
    blocking: tuple[str, ...]
    warnings: tuple[str, ...]
    ready: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "blocking": list(self.blocking),
            "warnings": list(self.warnings),
            "checks": [
                {
                    "key": c.key,
                    "label": c.label,
                    "status": c.status,
                    "detail": c.detail,
                    "blocking": c.blocking,
                    "acknowledged": c.acknowledged,
                }
                for c in self.checks
            ],
        }


_AUTH_LABELS = {
    "spf": "SPF record",
    "dkim": "DKIM signature",
    "dmarc": "DMARC policy",
    "mx": "MX records",
    "tls": "SMTP TLS",
    "sender_alignment": "Sender alignment",
}


def evaluate_readiness(
    *,
    connection_valid: bool,
    transport: Transport,
    tls_valid: bool | None,
    sender_identity_verified: bool,
    auth_checks: Mapping[str, str],
    auth_policy: AuthPolicy,
    global_sending_enabled: bool,
    account_mode: AccountMode,
    circuit_state: CircuitState,
    credentials_valid: bool,
    acknowledged_warnings: Iterable[str] = (),
) -> ReadinessReport:
    """Compute the production readiness gate.

    A `warning` blocks production until the operator explicitly acknowledges
    that specific check key. `fail` and `unknown` always block and cannot be
    acknowledged away.
    """

    acked = set(acknowledged_warnings)
    checks: list[ReadinessCheck] = []

    def add(check: ReadinessCheck) -> None:
        checks.append(check)

    add(
        ReadinessCheck(
            key="connection",
            label="Provider connection",
            status="pass" if connection_valid else "fail",
            detail="Provider connection was validated."
            if connection_valid
            else "Run a connection test; the provider has not been validated.",
            blocking=not connection_valid,
        )
    )

    add(
        ReadinessCheck(
            key="credentials",
            label="Credentials",
            status="pass" if credentials_valid else "fail",
            detail="Credentials are present and not reported as revoked."
            if credentials_valid
            else "Credentials are missing, expired, or revoked. Reauthorize the account.",
            blocking=not credentials_valid,
        )
    )

    add(
        ReadinessCheck(
            key="sender_identity",
            label="Sender identity",
            status="pass" if sender_identity_verified else "fail",
            detail="Sending domain and mailbox identity are verified."
            if sender_identity_verified
            else "Verify the sending domain before enabling production.",
            blocking=not sender_identity_verified,
        )
    )

    if transport == "smtp":
        tls_ok = tls_valid is True
        add(
            ReadinessCheck(
                key="tls",
                label=_AUTH_LABELS["tls"],
                status="pass" if tls_ok else ("unknown" if tls_valid is None else "fail"),
                detail="SMTP endpoint presented a valid certificate."
                if tls_ok
                else "SMTP TLS has not been validated with certificate verification.",
                blocking=not tls_ok,
            )
        )

    for check_type in ("spf", "dkim", "dmarc", "mx"):
        requirement = auth_policy.requirement_for(check_type)
        if requirement == "off":
            continue
        raw_status = auth_checks.get(check_type, "unknown")
        status: CheckStatus = (
            raw_status if raw_status in {"pass", "warning", "fail", "unknown", "not_applicable"} else "unknown"
        )
        key = f"auth_{check_type}"
        label = _AUTH_LABELS.get(check_type, check_type.upper())

        if status == "pass":
            add(ReadinessCheck(key=key, label=label, status="pass", detail="Check passed."))
            continue

        if requirement == "required":
            if status == "warning" and key in acked:
                add(
                    ReadinessCheck(
                        key=key,
                        label=label,
                        status="warning",
                        detail="Warning explicitly acknowledged by the operator.",
                        acknowledged=True,
                    )
                )
            else:
                add(
                    ReadinessCheck(
                        key=key,
                        label=label,
                        status=status if status != "not_applicable" else "unknown",
                        detail="Required authentication check is not passing.",
                        blocking=True,
                    )
                )
        else:  # recommended
            if status in ("warning", "fail", "unknown") and key not in acked:
                add(
                    ReadinessCheck(
                        key=key,
                        label=label,
                        status="warning",
                        detail="Recommended check is not passing; acknowledge to proceed.",
                        blocking=True,
                    )
                )
            else:
                add(
                    ReadinessCheck(
                        key=key,
                        label=label,
                        status="warning",
                        detail="Recommended check acknowledged by the operator.",
                        acknowledged=key in acked,
                    )
                )

    add(
        ReadinessCheck(
            key="global_sending",
            label="Global sending switch",
            status="pass" if global_sending_enabled else "fail",
            detail="Global sending is enabled."
            if global_sending_enabled
            else "Global sending is paused for the whole application.",
            blocking=not global_sending_enabled,
        )
    )

    mode_ok = account_mode == "production"
    add(
        ReadinessCheck(
            key="account_mode",
            label="Account mode",
            status="pass" if mode_ok else "warning",
            detail="Account is in production mode."
            if mode_ok
            else f"Account mode is '{account_mode}'; production sending is off.",
            blocking=not mode_ok,
        )
    )

    circuit_ok = circuit_state == "closed"
    add(
        ReadinessCheck(
            key="circuit",
            label="Circuit breaker",
            status="pass" if circuit_ok else "fail",
            detail="Circuit is closed."
            if circuit_ok
            else f"Circuit is '{circuit_state}' and must be reset or allowed to close.",
            blocking=not circuit_ok,
        )
    )

    blocking = tuple(c.key for c in checks if c.blocking)
    warnings = tuple(c.key for c in checks if c.status == "warning" and not c.blocking)
    return ReadinessReport(
        checks=tuple(checks),
        blocking=blocking,
        warnings=warnings,
        ready=not blocking,
    )


# --------------------------------------------------------------------------
# Retry and outcome handling (spec section 7)
# --------------------------------------------------------------------------

DEFAULT_RETRY_SCHEDULE: tuple[int, ...] = (30, 120, 600, 1800, 7200)


def retry_delay_seconds(
    attempt_number: int,
    *,
    schedule: Sequence[int] = DEFAULT_RETRY_SCHEDULE,
    jitter_ratio: float = 0.2,
    rng: random.Random | None = None,
) -> int:
    """Exponential backoff with symmetric jitter.

    `attempt_number` is 1-based and identifies the attempt that just failed.
    The returned delay is always >= 1 second and never negative.
    """

    if attempt_number < 1:
        raise ValueError("attempt_number must be >= 1")
    if not schedule:
        raise ValueError("schedule must not be empty")
    if not 0.0 <= jitter_ratio < 1.0:
        raise ValueError("jitter_ratio must be within [0, 1)")

    index = min(attempt_number, len(schedule)) - 1
    base = schedule[index]
    generator = rng or random.SystemRandom()
    jitter = base * jitter_ratio * (generator.random() * 2.0 - 1.0)
    return max(1, int(round(base + jitter)))


@dataclass(frozen=True)
class SendDecision:
    action: Action
    retry_at: datetime | None = None
    reason: str = ""
    retry_after_seconds: int | None = None


def decide_outcome(
    retryability: Retryability,
    *,
    attempt_count: int,
    max_attempts: int,
    now: datetime,
    retry_after_seconds: int | None = None,
    expires_at: datetime | None = None,
    schedule: Sequence[int] = DEFAULT_RETRY_SCHEDULE,
    max_retry_after_seconds: int = 6 * 60 * 60,
    rng: random.Random | None = None,
) -> SendDecision:
    """Map a provider outcome to the next action.

    Rules:
    - permanent failures never retry;
    - auth failures pause the account, they do not burn retries;
    - unknown outcomes go to human review (never auto-resubmit);
    - rate limits honour the provider hint;
    - expired messages fail instead of being retried.
    """

    if retryability == "permanent":
        return SendDecision("fail", reason="permanent_failure")
    if retryability == "auth":
        return SendDecision("pause_account", reason="auth_failure")
    if retryability == "unknown":
        return SendDecision("needs_review", reason="ambiguous_outcome")

    if attempt_count >= max_attempts:
        return SendDecision("fail", reason="max_attempts_exhausted")

    if expires_at is not None and now >= expires_at:
        return SendDecision("fail", reason="expired_before_retry")

    delay = retry_delay_seconds(attempt_count, schedule=schedule, rng=rng)
    if retry_after_seconds is not None:
        delay = max(delay, min(int(retry_after_seconds), max_retry_after_seconds))

    retry_at = now + timedelta(seconds=delay)
    if expires_at is not None and retry_at >= expires_at:
        return SendDecision("fail", reason="expires_before_next_attempt")

    return SendDecision(
        "retry",
        retry_at=retry_at,
        reason=retryability,
        retry_after_seconds=retry_after_seconds,
    )


# --------------------------------------------------------------------------
# Circuit breaker (spec section 7)
# --------------------------------------------------------------------------


class CircuitTrigger(str, Enum):
    AUTH_FAILURE = "auth_failure"
    QUOTA_RESTRICTION = "quota_restriction"
    TLS_FAILURE = "tls_failure"
    TRANSIENT_STREAK = "transient_streak"
    MANUAL = "manual"
    COMPLAINT = "complaint"


@dataclass(frozen=True)
class CircuitDecision:
    state: CircuitState
    reason: str
    open_until: datetime | None = None
    requires_manual_reset: bool = False


_TRANSIENT_AUTO_CLOSE = timedelta(minutes=30)


def circuit_after_failure(
    trigger: CircuitTrigger,
    *,
    now: datetime,
    consecutive_transient: int = 0,
    transient_threshold: int = 5,
    forced: bool = False,
) -> CircuitDecision | None:
    """Return a new circuit state, or None when no change is required."""

    if forced:
        return CircuitDecision(
            state="open",
            reason=f"{trigger.value}: operator forced open",
            requires_manual_reset=True,
        )

    if trigger in (CircuitTrigger.AUTH_FAILURE, CircuitTrigger.COMPLAINT, CircuitTrigger.MANUAL):
        return CircuitDecision(
            state="open",
            reason=f"{trigger.value}: manual review required",
            requires_manual_reset=True,
        )

    if trigger in (CircuitTrigger.QUOTA_RESTRICTION, CircuitTrigger.TLS_FAILURE):
        return CircuitDecision(
            state="open",
            reason=f"{trigger.value}: manual review required",
            requires_manual_reset=True,
        )

    if trigger == CircuitTrigger.TRANSIENT_STREAK and consecutive_transient >= transient_threshold:
        return CircuitDecision(
            state="open",
            reason=f"transient_streak: {consecutive_transient} consecutive transient failures",
            open_until=now + _TRANSIENT_AUTO_CLOSE,
            requires_manual_reset=False,
        )

    return None


def circuit_after_success(state: CircuitState) -> CircuitDecision | None:
    if state == "closed":
        return None
    return CircuitDecision(state="closed", reason="probe succeeded")


def circuit_auto_close_allowed(decision: CircuitDecision, *, now: datetime) -> bool:
    return (
        decision.state == "open"
        and not decision.requires_manual_reset
        and decision.open_until is not None
        and now >= decision.open_until
    )


# --------------------------------------------------------------------------
# Suppression (spec section 7)
# --------------------------------------------------------------------------

SUPPRESSING_REASONS = frozenset({"hard_bounce", "complaint", "operator_suppression", "invalid_address"})


def suppression_blocks(reason: str | None, *, removed_at: datetime | None) -> bool:
    return reason in SUPPRESSING_REASONS and removed_at is None


# --------------------------------------------------------------------------
# Test-recipient safety
# --------------------------------------------------------------------------


def is_diagnostic_test_subject(subject: str) -> bool:
    return subject.strip().upper().startswith("[DELIVERABILITY TEST")


def can_send_test_to(*, recipient_enabled: bool, recipient_verified: bool, allowlisted: bool) -> bool:
    return recipient_verified and recipient_enabled and allowlisted
