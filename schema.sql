-- Private Email Deliverability Platform — PostgreSQL schema v1.0
-- PostgreSQL 16+. Secrets themselves MUST NOT be stored in this database.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE email_accounts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider            text NOT NULL CHECK (provider IN ('google_gmail', 'google_workspace', 'microsoft_365', 'outlook_com', 'custom_smtp')),
    transport           text NOT NULL CHECK (transport IN ('gmail_api', 'microsoft_graph', 'smtp')),
    mailbox_email       text NOT NULL,
    display_name        text,
    sending_domain      text,
    state               text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'connected', 'auth_required', 'error', 'disabled')),
    connection_tested_at timestamptz,
    last_error_code     text,
    last_error_summary  text,
    provider_metadata   jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (
      (provider IN ('google_gmail', 'google_workspace') AND transport IN ('gmail_api', 'smtp')) OR
      (provider IN ('microsoft_365', 'outlook_com') AND transport IN ('microsoft_graph', 'smtp')) OR
      (provider = 'custom_smtp' AND transport = 'smtp')
    )
);
CREATE UNIQUE INDEX email_accounts_mailbox_lower_uq ON email_accounts (lower(mailbox_email));
CREATE INDEX email_accounts_state_idx ON email_accounts (state);

-- Points to a secret manager, OS keyring, or encrypted local vault. Never put tokens/passwords in this table.
CREATE TABLE provider_credentials (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id          uuid NOT NULL UNIQUE REFERENCES email_accounts(id) ON DELETE CASCADE,
    secret_backend      text NOT NULL CHECK (secret_backend IN ('os_keyring', 'docker_secret', 'cloud_secret_manager', 'encrypted_local_vault')),
    secret_ref          text NOT NULL,
    credential_version  integer NOT NULL DEFAULT 1 CHECK (credential_version > 0),
    granted_scopes      text[] NOT NULL DEFAULT ARRAY[]::text[],
    access_token_expires_at timestamptz,
    last_refresh_at     timestamptz,
    last_validated_at   timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- Short-lived OAuth/PKCE state. Single-use; expires quickly.
CREATE TABLE oauth_flows (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    state               text NOT NULL UNIQUE,
    provider            text NOT NULL CHECK (provider IN ('google_gmail', 'google_workspace', 'microsoft_365', 'outlook_com')),
    code_verifier       text NOT NULL,
    redirect_uri        text NOT NULL,
    requested_scopes    text[] NOT NULL DEFAULT ARRAY[]::text[],
    account_id          uuid REFERENCES email_accounts(id) ON DELETE SET NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    expires_at          timestamptz NOT NULL,
    consumed_at         timestamptz,
    CHECK (expires_at > created_at)
);
CREATE INDEX oauth_flows_expires_idx ON oauth_flows (expires_at);

CREATE TABLE send_policies (
    account_id          uuid PRIMARY KEY REFERENCES email_accounts(id) ON DELETE CASCADE,
    mode                text NOT NULL DEFAULT 'test_only' CHECK (mode IN ('test_only', 'production', 'paused')),
    production_enabled  boolean NOT NULL DEFAULT false,
    max_per_minute      integer NOT NULL DEFAULT 3 CHECK (max_per_minute > 0),
    max_rolling_24h     integer NOT NULL DEFAULT 25 CHECK (max_rolling_24h > 0),
    max_attempts        integer NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 10),
    max_in_flight       integer NOT NULL DEFAULT 1 CHECK (max_in_flight BETWEEN 1 AND 5),
    circuit_state       text NOT NULL DEFAULT 'closed' CHECK (circuit_state IN ('closed', 'open', 'half_open')),
    circuit_open_until  timestamptz,
    circuit_reason      text,
    require_auth_checks boolean NOT NULL DEFAULT true,
    require_test_recipient_allowlist boolean NOT NULL DEFAULT true,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CHECK ((mode <> 'production') OR production_enabled),
    CHECK ((circuit_state <> 'open') OR circuit_reason IS NOT NULL)
);

CREATE TABLE allowed_test_recipients (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    recipient_email     text NOT NULL,
    label               text,
    verification_method text NOT NULL CHECK (verification_method IN ('operator_confirmed', 'challenge_link', 'provider_owned_mailbox')),
    verified_at         timestamptz NOT NULL,
    is_enabled          boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX allowed_test_recipients_email_lower_uq ON allowed_test_recipients (lower(recipient_email));

CREATE TABLE email_messages (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id          uuid NOT NULL REFERENCES email_accounts(id) ON DELETE RESTRICT,
    kind                text NOT NULL CHECK (kind IN ('transactional', 'diagnostic_test')),
    status              text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sending', 'retry_wait', 'submitted', 'delivered', 'bounced', 'complained', 'failed', 'needs_review', 'cancelled', 'expired')),
    idempotency_key     text,
    request_fingerprint text,
    to_email            text NOT NULL,
    from_email          text NOT NULL,
    reply_to_email      text,
    subject             text NOT NULL,
    text_body           text,
    html_body           text,
    template_key        text,
    template_version    integer,
    scheduled_at        timestamptz NOT NULL DEFAULT now(),
    expires_at          timestamptz,
    lease_owner         text,
    lease_expires_at    timestamptz,
    next_attempt_at     timestamptz,
    attempt_count       integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    attempt_offset      integer NOT NULL DEFAULT 0 CHECK (attempt_offset >= 0),
    provider_message_id text,
    submitted_at        timestamptz,
    delivery_evidence_at timestamptz,
    final_error_code    text,
    final_error_summary text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (expires_at IS NULL OR expires_at > created_at)
);
CREATE UNIQUE INDEX email_messages_idempotency_uq ON email_messages (account_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX email_messages_queue_idx ON email_messages (scheduled_at, next_attempt_at, created_at)
    WHERE status IN ('queued', 'retry_wait');
CREATE INDEX email_messages_account_created_idx ON email_messages (account_id, created_at DESC);
CREATE INDEX email_messages_provider_id_idx ON email_messages (account_id, provider_message_id) WHERE provider_message_id IS NOT NULL;
CREATE INDEX email_messages_recipient_lower_idx ON email_messages (lower(to_email), created_at DESC);

CREATE TABLE delivery_attempts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id          uuid NOT NULL REFERENCES email_messages(id) ON DELETE CASCADE,
    attempt_number      integer NOT NULL CHECK (attempt_number > 0),
    provider            text NOT NULL,
    outcome             text NOT NULL CHECK (outcome IN ('accepted', 'transient_failure', 'permanent_failure', 'auth_failure', 'rate_limited', 'unknown_outcome')),
    started_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz,
    duration_ms         integer CHECK (duration_ms IS NULL OR duration_ms >= 0),
    http_status         integer,
    smtp_code           integer,
    enhanced_smtp_code  text,
    provider_message_id text,
    retry_after_seconds integer CHECK (retry_after_seconds IS NULL OR retry_after_seconds >= 0),
    diagnostic_code     text,
    sanitized_detail    text,
    response_metadata   jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (message_id, attempt_number)
);
CREATE INDEX delivery_attempts_recent_idx ON delivery_attempts (started_at DESC, outcome);

-- Reserve before calling a provider; mark consumed only on accepted submission; release on confirmed failure.
CREATE TABLE quota_reservations (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id          uuid NOT NULL REFERENCES email_accounts(id) ON DELETE CASCADE,
    message_id          uuid NOT NULL UNIQUE REFERENCES email_messages(id) ON DELETE CASCADE,
    status              text NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved', 'consumed', 'released')),
    reserved_at         timestamptz NOT NULL DEFAULT now(),
    consumed_at         timestamptz,
    released_at         timestamptz,
    release_reason      text,
    CHECK ((status <> 'consumed') OR consumed_at IS NOT NULL),
    CHECK ((status <> 'released') OR released_at IS NOT NULL)
);
CREATE INDEX quota_reservations_account_reserved_idx ON quota_reservations (account_id, reserved_at DESC, status);
CREATE INDEX quota_reservations_consumed_idx ON quota_reservations (account_id, consumed_at DESC) WHERE status = 'consumed';

CREATE TABLE provider_events (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id          uuid NOT NULL REFERENCES email_accounts(id) ON DELETE CASCADE,
    message_id          uuid REFERENCES email_messages(id) ON DELETE SET NULL,
    provider            text NOT NULL,
    provider_event_id   text,
    event_type          text NOT NULL CHECK (event_type IN ('delivery', 'bounce', 'complaint', 'deferred', 'rejected', 'authentication', 'quota', 'unknown')),
    event_at            timestamptz NOT NULL,
    received_at         timestamptz NOT NULL DEFAULT now(),
    recipient_email     text,
    diagnostic_code     text,
    sanitized_detail    text,
    event_metadata      jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE UNIQUE INDEX provider_events_external_id_uq ON provider_events (account_id, provider_event_id) WHERE provider_event_id IS NOT NULL;
CREATE INDEX provider_events_message_idx ON provider_events (message_id, event_at DESC);
CREATE INDEX provider_events_type_idx ON provider_events (account_id, event_type, event_at DESC);

CREATE TABLE suppressions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    recipient_email     text NOT NULL,
    reason              text NOT NULL CHECK (reason IN ('hard_bounce', 'complaint', 'operator_suppression', 'invalid_address')),
    source_account_id   uuid REFERENCES email_accounts(id) ON DELETE SET NULL,
    source_message_id   uuid REFERENCES email_messages(id) ON DELETE SET NULL,
    evidence_event_id   uuid REFERENCES provider_events(id) ON DELETE SET NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    removed_at          timestamptz,
    removed_by          text,
    removal_reason      text
);
CREATE UNIQUE INDEX suppressions_active_email_lower_uq ON suppressions (lower(recipient_email)) WHERE removed_at IS NULL;
CREATE INDEX suppressions_email_lookup_idx ON suppressions (lower(recipient_email), removed_at);

CREATE TABLE domain_auth_checks (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id          uuid REFERENCES email_accounts(id) ON DELETE CASCADE,
    domain              text NOT NULL,
    check_type          text NOT NULL CHECK (check_type IN ('spf', 'dkim', 'dmarc', 'mx', 'tls', 'sender_alignment')),
    selector            text,
    status              text NOT NULL CHECK (status IN ('pass', 'warning', 'fail', 'unknown', 'not_applicable')),
    observed_value      text,
    evidence            jsonb NOT NULL DEFAULT '{}'::jsonb,
    checked_at          timestamptz NOT NULL DEFAULT now(),
    expires_at          timestamptz
);
CREATE INDEX domain_auth_checks_latest_idx ON domain_auth_checks (lower(domain), check_type, checked_at DESC);
CREATE INDEX domain_auth_checks_account_idx ON domain_auth_checks (account_id, checked_at DESC);

CREATE TABLE test_runs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id          uuid NOT NULL REFERENCES email_accounts(id) ON DELETE RESTRICT,
    status              text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'completed', 'completed_with_errors', 'failed', 'cancelled')),
    initiated_by        text NOT NULL DEFAULT 'operator',
    test_profile        text NOT NULL DEFAULT 'basic_delivery' CHECK (test_profile IN ('basic_delivery', 'authentication', 'latency')),
    started_at          timestamptz,
    completed_at        timestamptz,
    summary             jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX test_runs_account_created_idx ON test_runs (account_id, created_at DESC);

CREATE TABLE test_run_results (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    test_run_id         uuid NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
    allowed_recipient_id uuid NOT NULL REFERENCES allowed_test_recipients(id) ON DELETE RESTRICT,
    message_id          uuid REFERENCES email_messages(id) ON DELETE SET NULL,
    status              text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'submitted', 'observed_received', 'auth_pass', 'warning', 'failed', 'timed_out')),
    observed_at         timestamptz,
    observed_inbox      text,
    authentication_results jsonb NOT NULL DEFAULT '{}'::jsonb,
    latency_ms          integer CHECK (latency_ms IS NULL OR latency_ms >= 0),
    result_detail       text,
    UNIQUE (test_run_id, allowed_recipient_id)
);

CREATE TABLE safety_events (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id          uuid REFERENCES email_accounts(id) ON DELETE SET NULL,
    message_id          uuid REFERENCES email_messages(id) ON DELETE SET NULL,
    severity            text NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    event_type          text NOT NULL,
    action_taken        text NOT NULL,
    details             jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at         timestamptz NOT NULL DEFAULT now(),
    acknowledged_at     timestamptz,
    acknowledged_by     text
);
CREATE INDEX safety_events_recent_idx ON safety_events (occurred_at DESC, severity);

CREATE TABLE audit_log (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor               text NOT NULL,
    action              text NOT NULL,
    target_type         text,
    target_id           uuid,
    result              text NOT NULL DEFAULT 'success' CHECK (result IN ('success', 'denied', 'failure')),
    request_id          text,
    source_ip           inet,
    details             jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_recent_idx ON audit_log (created_at DESC);
CREATE INDEX audit_log_target_idx ON audit_log (target_type, target_id, created_at DESC);

CREATE TABLE diagnostic_reports (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id          uuid REFERENCES email_accounts(id) ON DELETE CASCADE,
    message_id          uuid REFERENCES email_messages(id) ON DELETE SET NULL,
    source_attempt_id   uuid REFERENCES delivery_attempts(id) ON DELETE SET NULL,
    engine              text NOT NULL CHECK (engine IN ('rules', 'llm')),
    summary             text NOT NULL,
    likely_causes       jsonb NOT NULL DEFAULT '[]'::jsonb,
    recommended_actions jsonb NOT NULL DEFAULT '[]'::jsonb,
    confidence          text NOT NULL DEFAULT 'medium' CHECK (confidence IN ('low', 'medium', 'high')),
    needs_operator_action boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX diagnostic_reports_recent_idx ON diagnostic_reports (account_id, created_at DESC);

-- Global kill switch and coarse app settings. Values contain policy only, never provider secrets.
CREATE TABLE app_settings (
    setting_key         text PRIMARY KEY,
    setting_value       jsonb NOT NULL,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    updated_by          text NOT NULL
);
INSERT INTO app_settings (setting_key, setting_value, updated_by)
VALUES ('global_sending_enabled', 'false'::jsonb, 'schema-init')
ON CONFLICT (setting_key) DO NOTHING;

-- Operational notes:
-- 1) Update updated_at in the application/service layer or with a shared trigger.
-- 2) The application must enforce state-transition rules in transactions.
-- 3) Use row locks on send_policies while making quota reservations.
-- 4) Do not use provider message acceptance as delivery evidence.
-- 5) Encrypt database volumes/backups, restrict access, and configure retention.
