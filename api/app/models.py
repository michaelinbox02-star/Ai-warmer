"""SQLAlchemy models mirroring `db/schema.sql`.

`db/schema.sql` is the canonical DDL. These mappings exist so application code
is typed; they are not used to create the schema (Alembic owns that).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSON_EMPTY_OBJECT = text("'{}'::jsonb")
JSON_EMPTY_ARRAY = text("'[]'::jsonb")
NOW = text("now()")


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


class EmailAccount(Base):
    __tablename__ = "email_accounts"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('google_gmail', 'google_workspace', 'microsoft_365', 'outlook_com', 'custom_smtp')",
            name="email_accounts_provider_chk",
        ),
        CheckConstraint(
            "transport IN ('gmail_api', 'microsoft_graph', 'smtp')",
            name="email_accounts_transport_chk",
        ),
        CheckConstraint(
            "state IN ('pending', 'connected', 'auth_required', 'error', 'disabled')",
            name="email_accounts_state_chk",
        ),
        CheckConstraint(
            "(provider IN ('google_gmail', 'google_workspace') AND transport IN ('gmail_api', 'smtp')) OR "
            "(provider IN ('microsoft_365', 'outlook_com') AND transport IN ('microsoft_graph', 'smtp')) OR "
            "(provider = 'custom_smtp' AND transport = 'smtp')",
            name="email_accounts_provider_transport_chk",
        ),
        Index(
            "email_accounts_mailbox_lower_uq",
            text("lower(mailbox_email)"),
            unique=True,
        ),
        Index("email_accounts_state_idx", "state"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    transport: Mapped[str] = mapped_column(Text, nullable=False)
    mailbox_email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    sending_domain: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending", server_default=text("'pending'"))
    connection_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(Text)
    last_error_summary: Mapped[str | None] = mapped_column(Text)
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class ProviderCredential(Base):
    __tablename__ = "provider_credentials"
    __table_args__ = (
        UniqueConstraint("account_id", name="provider_credentials_account_id_key"),
        CheckConstraint(
            "secret_backend IN ('os_keyring', 'docker_secret', 'cloud_secret_manager', 'encrypted_local_vault')",
            name="provider_credentials_backend_chk",
        ),
        CheckConstraint("credential_version > 0", name="provider_credentials_version_chk"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    secret_backend: Mapped[str] = mapped_column(Text, nullable=False)
    secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    credential_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    granted_scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("ARRAY[]::text[]")
    )
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class SendPolicy(Base):
    __tablename__ = "send_policies"
    __table_args__ = (
        CheckConstraint("mode IN ('test_only', 'production', 'paused')", name="send_policies_mode_chk"),
        CheckConstraint("max_per_minute > 0", name="send_policies_per_minute_chk"),
        CheckConstraint("max_rolling_24h > 0", name="send_policies_rolling_chk"),
        CheckConstraint("max_attempts BETWEEN 1 AND 10", name="send_policies_attempts_chk"),
        CheckConstraint("max_in_flight BETWEEN 1 AND 5", name="send_policies_in_flight_chk"),
        CheckConstraint(
            "circuit_state IN ('closed', 'open', 'half_open')", name="send_policies_circuit_chk"
        ),
        CheckConstraint("(mode <> 'production') OR production_enabled", name="send_policies_production_chk"),
        CheckConstraint(
            "(circuit_state <> 'open') OR circuit_reason IS NOT NULL",
            name="send_policies_circuit_reason_chk",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    mode: Mapped[str] = mapped_column(
        Text, nullable=False, default="test_only", server_default=text("'test_only'")
    )
    production_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    max_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default=text("3"))
    max_rolling_24h: Mapped[int] = mapped_column(Integer, nullable=False, default=25, server_default=text("25"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default=text("5"))
    max_in_flight: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    circuit_state: Mapped[str] = mapped_column(
        Text, nullable=False, default="closed", server_default=text("'closed'")
    )
    circuit_open_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    circuit_reason: Mapped[str | None] = mapped_column(Text)
    require_auth_checks: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    require_test_recipient_allowlist: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class AllowedTestRecipient(Base):
    __tablename__ = "allowed_test_recipients"
    __table_args__ = (
        CheckConstraint(
            "verification_method IN ('operator_confirmed', 'challenge_link', 'provider_owned_mailbox')",
            name="allowed_recipients_method_chk",
        ),
        Index("allowed_test_recipients_email_lower_uq", text("lower(recipient_email)"), unique=True),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    recipient_email: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    verification_method: Mapped[str] = mapped_column(Text, nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class EmailMessage(Base):
    __tablename__ = "email_messages"
    __table_args__ = (
        CheckConstraint("kind IN ('transactional', 'diagnostic_test')", name="email_messages_kind_chk"),
        CheckConstraint(
            "status IN ('queued', 'sending', 'retry_wait', 'submitted', 'delivered', 'bounced', "
            "'complained', 'failed', 'needs_review', 'cancelled', 'expired')",
            name="email_messages_status_chk",
        ),
        CheckConstraint("attempt_count >= 0", name="email_messages_attempts_chk"),
        CheckConstraint("attempt_offset >= 0", name="email_messages_attempt_offset_chk"),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at", name="email_messages_expiry_chk"
        ),
        Index(
            "email_messages_idempotency_uq",
            "account_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index(
            "email_messages_queue_idx",
            "scheduled_at",
            "next_attempt_at",
            "created_at",
            postgresql_where=text("status IN ('queued', 'retry_wait')"),
        ),
        Index("email_messages_account_created_idx", "account_id", text("created_at DESC")),
        Index(
            "email_messages_provider_id_idx",
            "account_id",
            "provider_message_id",
            postgresql_where=text("provider_message_id IS NOT NULL"),
        ),
        Index("email_messages_recipient_lower_idx", text("lower(to_email)"), text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="transactional")
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="queued", server_default=text("'queued'")
    )
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    request_fingerprint: Mapped[str | None] = mapped_column(Text)
    to_email: Mapped[str] = mapped_column(Text, nullable=False)
    from_email: Mapped[str] = mapped_column(Text, nullable=False)
    reply_to_email: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    text_body: Mapped[str | None] = mapped_column(Text)
    html_body: Mapped[str | None] = mapped_column(Text)
    template_key: Mapped[str | None] = mapped_column(Text)
    template_version: Mapped[int | None] = mapped_column(Integer)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # Numbering offset preserved across operator requeues. `attempt_count`
    # remains the retry budget for the current cycle, while the durable
    # attempt row number is `attempt_offset + attempt_count`.
    attempt_offset: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_evidence_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    final_error_code: Mapped[str | None] = mapped_column(Text)
    final_error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"
    __table_args__ = (
        UniqueConstraint("message_id", "attempt_number", name="delivery_attempts_message_attempt_key"),
        CheckConstraint("attempt_number > 0", name="delivery_attempts_number_chk"),
        CheckConstraint(
            "outcome IN ('accepted', 'transient_failure', 'permanent_failure', 'auth_failure', "
            "'rate_limited', 'unknown_outcome')",
            name="delivery_attempts_outcome_chk",
        ),
        Index("delivery_attempts_recent_idx", text("started_at DESC"), "outcome"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_messages.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    http_status: Mapped[int | None] = mapped_column(Integer)
    smtp_code: Mapped[int | None] = mapped_column(Integer)
    enhanced_smtp_code: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    retry_after_seconds: Mapped[int | None] = mapped_column(Integer)
    diagnostic_code: Mapped[str | None] = mapped_column(Text)
    sanitized_detail: Mapped[str | None] = mapped_column(Text)
    response_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )


class QuotaReservation(Base):
    __tablename__ = "quota_reservations"
    __table_args__ = (
        UniqueConstraint("message_id", name="quota_reservations_message_id_key"),
        CheckConstraint("status IN ('reserved', 'consumed', 'released')", name="quota_status_chk"),
        CheckConstraint("(status <> 'consumed') OR consumed_at IS NOT NULL", name="quota_consumed_chk"),
        CheckConstraint("(status <> 'released') OR released_at IS NOT NULL", name="quota_released_chk"),
        Index("quota_reservations_account_reserved_idx", "account_id", text("reserved_at DESC"), "status"),
        Index(
            "quota_reservations_consumed_idx",
            "account_id",
            text("consumed_at DESC"),
            postgresql_where=text("status = 'consumed'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_accounts.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_messages.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="reserved", server_default=text("'reserved'")
    )
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(Text)


class ProviderEvent(Base):
    __tablename__ = "provider_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('delivery', 'bounce', 'complaint', 'deferred', 'rejected', "
            "'authentication', 'quota', 'unknown')",
            name="provider_events_type_chk",
        ),
        Index(
            "provider_events_external_id_uq",
            "account_id",
            "provider_event_id",
            unique=True,
            postgresql_where=text("provider_event_id IS NOT NULL"),
        ),
        Index("provider_events_message_idx", "message_id", text("event_at DESC")),
        Index("provider_events_type_idx", "account_id", "event_type", text("event_at DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_accounts.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_messages.id", ondelete="SET NULL")
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_event_id: Mapped[str | None] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    recipient_email: Mapped[str | None] = mapped_column(Text)
    diagnostic_code: Mapped[str | None] = mapped_column(Text)
    sanitized_detail: Mapped[str | None] = mapped_column(Text)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )


class Suppression(Base):
    __tablename__ = "suppressions"
    __table_args__ = (
        CheckConstraint(
            "reason IN ('hard_bounce', 'complaint', 'operator_suppression', 'invalid_address')",
            name="suppressions_reason_chk",
        ),
        Index(
            "suppressions_active_email_lower_uq",
            text("lower(recipient_email)"),
            unique=True,
            postgresql_where=text("removed_at IS NULL"),
        ),
        Index("suppressions_email_lookup_idx", text("lower(recipient_email)"), "removed_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    recipient_email: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_accounts.id", ondelete="SET NULL")
    )
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_messages.id", ondelete="SET NULL")
    )
    evidence_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_events.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_by: Mapped[str | None] = mapped_column(Text)
    removal_reason: Mapped[str | None] = mapped_column(Text)


class DomainAuthCheck(Base):
    __tablename__ = "domain_auth_checks"
    __table_args__ = (
        CheckConstraint(
            "check_type IN ('spf', 'dkim', 'dmarc', 'mx', 'tls', 'sender_alignment')",
            name="domain_checks_type_chk",
        ),
        CheckConstraint(
            "status IN ('pass', 'warning', 'fail', 'unknown', 'not_applicable')",
            name="domain_checks_status_chk",
        ),
        Index(
            "domain_auth_checks_latest_idx",
            text("lower(domain)"),
            "check_type",
            text("checked_at DESC"),
        ),
        Index("domain_auth_checks_account_idx", "account_id", text("checked_at DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_accounts.id", ondelete="CASCADE")
    )
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    check_type: Mapped[str] = mapped_column(Text, nullable=False)
    selector: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    observed_value: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TestRun(Base):
    __tablename__ = "test_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'completed_with_errors', 'failed', 'cancelled')",
            name="test_runs_status_chk",
        ),
        CheckConstraint(
            "test_profile IN ('basic_delivery', 'authentication', 'latency')",
            name="test_runs_profile_chk",
        ),
        Index("test_runs_account_created_idx", "account_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="queued", server_default=text("'queued'")
    )
    initiated_by: Mapped[str] = mapped_column(
        Text, nullable=False, default="operator", server_default=text("'operator'")
    )
    test_profile: Mapped[str] = mapped_column(
        Text, nullable=False, default="basic_delivery", server_default=text("'basic_delivery'")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class TestRunResult(Base):
    __tablename__ = "test_run_results"
    __table_args__ = (
        UniqueConstraint("test_run_id", "allowed_recipient_id", name="test_results_run_recipient_key"),
        CheckConstraint(
            "status IN ('pending', 'submitted', 'observed_received', 'auth_pass', 'warning', "
            "'failed', 'timed_out')",
            name="test_results_status_chk",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    test_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False
    )
    allowed_recipient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("allowed_test_recipients.id", ondelete="RESTRICT"),
        nullable=False,
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_messages.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default=text("'pending'")
    )
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_inbox: Mapped[str | None] = mapped_column(Text)
    authentication_results: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    result_detail: Mapped[str | None] = mapped_column(Text)


class SafetyEvent(Base):
    __tablename__ = "safety_events"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('info', 'warning', 'critical')", name="safety_events_severity_chk"
        ),
        Index("safety_events_recent_idx", text("occurred_at DESC"), "severity"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_accounts.id", ondelete="SET NULL")
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_messages.id", ondelete="SET NULL")
    )
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    action_taken: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(
            "result IN ('success', 'denied', 'failure')", name="audit_result_chk"
        ),
        Index("audit_log_recent_idx", text("created_at DESC")),
        Index("audit_log_target_idx", "target_type", "target_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    result: Mapped[str] = mapped_column(
        Text, nullable=False, default="success", server_default=text("'success'")
    )
    request_id: Mapped[str | None] = mapped_column(Text)
    source_ip: Mapped[str | None] = mapped_column(INET)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=JSON_EMPTY_OBJECT
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class DiagnosticReport(Base):
    __tablename__ = "diagnostic_reports"
    __table_args__ = (
        CheckConstraint("engine IN ('rules', 'llm')", name="diagnostic_engine_chk"),
        CheckConstraint(
            "confidence IN ('low', 'medium', 'high')", name="diagnostic_confidence_chk"
        ),
        Index("diagnostic_reports_recent_idx", "account_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_accounts.id", ondelete="CASCADE")
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_messages.id", ondelete="SET NULL")
    )
    source_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("delivery_attempts.id", ondelete="SET NULL")
    )
    engine: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    likely_causes: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=JSON_EMPTY_ARRAY
    )
    recommended_actions: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=JSON_EMPTY_ARRAY
    )
    confidence: Mapped[str] = mapped_column(
        Text, nullable=False, default="medium", server_default=text("'medium'")
    )
    needs_operator_action: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class AppSetting(Base):
    __tablename__ = "app_settings"

    setting_key: Mapped[str] = mapped_column(Text, primary_key=True)
    setting_value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)


class OAuthFlow(Base):
    """Short-lived OAuth/PKCE state (schema addition, migration 0002).

    State is single-use by construction: the row is consumed inside the same
    transaction that exchanges the code.
    """

    __tablename__ = "oauth_flows"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('google_gmail', 'google_workspace', 'microsoft_365', 'outlook_com')",
            name="oauth_flows_provider_chk",
        ),
        Index("oauth_flows_expires_idx", "expires_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    state: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    code_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    requested_scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_accounts.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
