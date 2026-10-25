"""Provider abstraction (spec section 4).

Adapters never decide policy. They report what happened and return a
retryability classification; the sender applies policy and state transitions.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from app.core.errors import CapabilityNotSupported
from app.core.policy_rules import Retryability


@dataclass(frozen=True)
class ProviderCapabilities:
    send: bool
    delivery_events: bool
    inbox_read: bool
    oauth: bool
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "send": self.send,
            "delivery_events": self.delivery_events,
            "inbox_read": self.inbox_read,
            "oauth": self.oauth,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ProviderContext:
    """Everything an adapter needs, with secrets decrypted in memory only."""

    account_id: uuid.UUID
    provider: str
    transport: str
    mailbox_email: str
    display_name: str | None
    sending_domain: str | None
    metadata: Mapping[str, Any]
    secret: Mapping[str, Any]

    def meta(self, key: str, default: Any = None) -> Any:
        return self.metadata.get(key, default)

    def secret_value(self, key: str) -> str | None:
        value = self.secret.get(key)
        return str(value) if value is not None else None


@dataclass(frozen=True)
class ConnectionTestResult:
    ok: bool
    stage: str
    code: str
    summary: str
    capabilities: ProviderCapabilities
    details: dict[str, Any] = field(default_factory=dict)
    credentials_update: Mapping[str, Any] | None = None
    access_token_expires_at: datetime | None = None


@dataclass(frozen=True)
class OutboundMessage:
    message_id: uuid.UUID
    from_email: str
    to_email: str
    subject: str
    text_body: str | None
    html_body: str | None
    from_name: str | None = None
    reply_to_email: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    kind: str = "transactional"
    test_run_id: uuid.UUID | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class ProviderSubmissionResult:
    accepted: bool
    retryability: Retryability
    submitted_at: datetime
    provider_message_id: str | None = None
    http_status: int | None = None
    smtp_code: int | None = None
    enhanced_smtp_code: str | None = None
    diagnostic_code: str | None = None
    detail: str | None = None
    retry_after_seconds: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    credentials_update: Mapping[str, Any] | None = None
    access_token_expires_at: datetime | None = None
    # Explicit hint so the worker does not have to infer circuit behaviour
    # from an error string.
    circuit_trigger: str | None = None

    @property
    def outcome(self) -> str:
        if self.accepted:
            return "accepted"
        return {
            "transient": "transient_failure",
            "permanent": "permanent_failure",
            "auth": "auth_failure",
            "rate_limited": "rate_limited",
            "unknown": "unknown_outcome",
        }[self.retryability]


@dataclass(frozen=True)
class CredentialRefreshResult:
    ok: bool
    code: str
    summary: str
    credentials_update: Mapping[str, Any] | None = None
    access_token_expires_at: datetime | None = None


@dataclass(frozen=True)
class ProviderEventRecord:
    provider_event_id: str | None
    event_type: str
    event_at: datetime
    recipient_email: str | None = None
    provider_message_id: str | None = None
    diagnostic_code: str | None = None
    detail: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderEventBatch:
    events: tuple[ProviderEventRecord, ...]
    next_cursor: str | None = None


class EmailProvider(ABC):
    name: str
    capabilities: ProviderCapabilities

    @abstractmethod
    async def validate_connection(self, ctx: ProviderContext) -> ConnectionTestResult:
        """Validate DNS, TLS, and authentication without sending mail."""

    @abstractmethod
    async def send(self, ctx: ProviderContext, message: OutboundMessage) -> ProviderSubmissionResult:
        """Submit one message. Acceptance is not delivery evidence."""

    @abstractmethod
    async def refresh_credentials(self, ctx: ProviderContext) -> CredentialRefreshResult:
        """Refresh short-lived credentials if the provider supports it."""

    async def fetch_events(
        self, ctx: ProviderContext, cursor: str | None = None
    ) -> ProviderEventBatch:
        raise CapabilityNotSupported(
            f"{self.name} does not support delivery-event polling in this release",
            details={"capability": "delivery_events"},
        )
