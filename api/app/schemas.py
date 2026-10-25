"""Request/response schemas.

Secrets are write-only: there is no response model that can serialize a
password, token, or secret reference.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

ProviderName = Literal["google_gmail", "google_workspace", "microsoft_365", "outlook_com", "custom_smtp"]
TransportName = Literal["gmail_api", "microsoft_graph", "smtp"]
MessageKind = Literal["transactional", "diagnostic_test"]


def _require_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("Timestamps must include a timezone (UTC recommended)")
    return value.astimezone(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class LoginRequest(StrictModel):
    password: str = Field(min_length=1, max_length=1024)


class SessionInfo(BaseModel):
    authenticated: bool
    actor: str | None = None
    method: str | None = None
    csrf_token: str | None = None
    session_expires_in: int | None = None


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


class SmtpSettings(StrictModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=587, ge=1, le=65535)
    security: Literal["starttls", "tls"] = "starttls"
    username: str | None = Field(default=None, max_length=320)
    password: str = Field(min_length=1, max_length=4096, repr=False)


class AccountCreateRequest(StrictModel):
    provider: ProviderName
    transport: TransportName
    mailbox_email: EmailStr
    display_name: str | None = Field(default=None, max_length=200)
    sending_domain: str | None = Field(default=None, max_length=253)
    smtp: SmtpSettings | None = None
    dkim_selectors: list[str] | None = Field(default=None, max_length=10)
    auth_policy: dict[str, Literal["required", "recommended", "off"]] | None = None
    allowed_from: list[EmailStr] = Field(default_factory=list, max_length=20)

    @field_validator("sending_domain")
    @classmethod
    def _domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        domain = value.strip().lower().rstrip(".")
        if not domain or "." not in domain or " " in domain:
            raise ValueError("sending_domain must be a valid domain")
        return domain

    @model_validator(mode="after")
    def _compatible(self) -> "AccountCreateRequest":
        allowed = {
            "google_gmail": {"gmail_api", "smtp"},
            "google_workspace": {"gmail_api", "smtp"},
            "microsoft_365": {"microsoft_graph", "smtp"},
            "outlook_com": {"microsoft_graph", "smtp"},
            "custom_smtp": {"smtp"},
        }
        if self.transport not in allowed[self.provider]:
            raise ValueError(f"{self.provider} does not support the {self.transport} transport")
        if self.transport == "smtp" and self.smtp is None:
            raise ValueError("smtp settings are required for the SMTP transport")
        if self.transport != "smtp" and self.smtp is not None:
            raise ValueError("smtp settings must not be supplied for provider API transports")
        return self


class AccountDisableRequest(StrictModel):
    reason: str = Field(min_length=3, max_length=500)


class SmtpCredentialUpdateRequest(StrictModel):
    password: str = Field(min_length=1, max_length=4096, repr=False)
    username: str | None = Field(default=None, max_length=320)


class ModeUpdateRequest(StrictModel):
    mode: Literal["test_only", "production", "paused"]
    acknowledgements: list[str] = Field(default_factory=list, max_length=20)
    reason: str | None = Field(default=None, max_length=500)


class PolicyUpdateRequest(StrictModel):
    max_per_minute: int | None = Field(default=None, ge=1, le=600)
    max_rolling_24h: int | None = Field(default=None, ge=1, le=100_000)
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    max_in_flight: int | None = Field(default=None, ge=1, le=5)
    require_auth_checks: bool | None = None
    require_test_recipient_allowlist: bool | None = None
    reason: str | None = Field(default=None, max_length=500)


class CircuitResetRequest(StrictModel):
    reason: str = Field(min_length=3, max_length=500)


class AcknowledgementRequest(StrictModel):
    keys: list[str] = Field(min_length=1, max_length=20)
    note: str | None = Field(default=None, max_length=500)


class SenderIdentityRequest(StrictModel):
    sending_domain: str = Field(min_length=3, max_length=253)
    verified: bool = True
    note: str | None = Field(default=None, max_length=500)

    @field_validator("sending_domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        domain = value.strip().lower().rstrip(".")
        if "." not in domain or " " in domain:
            raise ValueError("sending_domain must be a valid domain")
        return domain


# ---------------------------------------------------------------------------
# Domain checks
# ---------------------------------------------------------------------------


class DomainCheckRequest(StrictModel):
    check_types: list[
        Literal["spf", "dkim", "dmarc", "mx", "tls", "sender_alignment"]
    ] | None = Field(default=None, max_length=6)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class MessageCreateRequest(StrictModel):
    account_id: uuid.UUID
    to: EmailStr
    subject: str = Field(min_length=1, max_length=998)
    text_body: str | None = Field(default=None, max_length=200_000)
    html_body: str | None = Field(default=None, max_length=200_000)
    reply_to: EmailStr | None = None
    from_email: EmailStr | None = None
    template_key: str | None = Field(default=None, max_length=100)
    template_version: int | None = Field(default=None, ge=1, le=10_000)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _expiry(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value)

    @model_validator(mode="after")
    def _body(self) -> "MessageCreateRequest":
        if not self.text_body and not self.html_body:
            raise ValueError("Provide text_body, html_body, or both")
        if self.expires_at is not None and self.expires_at <= datetime.now(timezone.utc):
            raise ValueError("expires_at must be in the future")
        return self


class RequeueRequest(StrictModel):
    note: str = Field(min_length=5, max_length=500)
    confirm_resubmission_risk: bool

    @model_validator(mode="after")
    def _confirm(self) -> "RequeueRequest":
        if not self.confirm_resubmission_risk:
            raise ValueError("confirm_resubmission_risk must be true")
        return self


class CancelRequest(StrictModel):
    reason: str = Field(min_length=3, max_length=500)


class DiagnosisRequest(StrictModel):
    use_llm: bool = True


# ---------------------------------------------------------------------------
# Controlled tests
# ---------------------------------------------------------------------------


class TestRecipientCreateRequest(StrictModel):
    recipient_email: EmailStr
    label: str | None = Field(default=None, max_length=100)
    verification_method: Literal[
        "operator_confirmed", "challenge_link", "provider_owned_mailbox"
    ] = "operator_confirmed"
    attest_authorized: bool

    @model_validator(mode="after")
    def _attest(self) -> "TestRecipientCreateRequest":
        if not self.attest_authorized:
            raise ValueError(
                "You must confirm that you control or are authorized to test this mailbox"
            )
        return self


class TestRunCreateRequest(StrictModel):
    account_id: uuid.UUID
    profile: Literal["basic_delivery", "authentication", "latency"] = "basic_delivery"
    recipient_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    recipient_emails: list[EmailStr] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _recipients(self) -> "TestRunCreateRequest":
        if not self.recipient_ids and not self.recipient_emails:
            raise ValueError("Provide at least one recipient id or email")
        return self


class TestRecipientStateRequest(StrictModel):
    enabled: bool
    reason: str | None = Field(default=None, max_length=500)


class ObservationRequest(StrictModel):
    raw_headers: str = Field(min_length=10, max_length=200_000)
    observed_inbox: str | None = Field(default=None, max_length=320)
    observed_at: datetime | None = None

    @field_validator("observed_at")
    @classmethod
    def _observed(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value)


# ---------------------------------------------------------------------------
# System / events
# ---------------------------------------------------------------------------


class PauseRequest(StrictModel):
    reason: str = Field(min_length=3, max_length=500)


class SystemEventRequest(StrictModel):
    account_id: uuid.UUID
    event_type: Literal[
        "delivery", "bounce", "complaint", "deferred", "rejected", "authentication", "quota", "unknown"
    ]
    event_at: datetime
    provider_event_id: str | None = Field(default=None, max_length=255)
    provider: str | None = Field(default=None, max_length=50)
    message_id: uuid.UUID | None = None
    provider_message_id: str | None = Field(default=None, max_length=255)
    recipient_email: EmailStr | None = None
    bounce_type: Literal["hard", "soft"] | None = None
    diagnostic_code: str | None = Field(default=None, max_length=100)
    detail: str | None = Field(default=None, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value)  # type: ignore[return-value]


class RetentionUpdateRequest(StrictModel):
    body_days: int = Field(ge=1, le=365)
    event_days: int = Field(ge=1, le=3650)
    audit_days: int = Field(ge=1, le=3650)
