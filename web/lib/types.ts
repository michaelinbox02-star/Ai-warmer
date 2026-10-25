export type AccountState = "pending" | "connected" | "auth_required" | "error" | "disabled";
export type AccountMode = "test_only" | "production" | "paused";
export type MessageStatus =
  | "queued"
  | "sending"
  | "retry_wait"
  | "submitted"
  | "delivered"
  | "bounced"
  | "complained"
  | "failed"
  | "needs_review"
  | "cancelled"
  | "expired";

export interface Policy {
  mode: AccountMode;
  production_enabled: boolean;
  max_per_minute: number;
  max_rolling_24h: number;
  max_attempts: number;
  max_in_flight: number;
  circuit: {
    state: "closed" | "open" | "half_open";
    open_until: string | null;
    reason: string | null;
    requires_manual_reset: boolean;
  };
  require_auth_checks: boolean;
  require_test_recipient_allowlist: boolean;
  updated_at: string | null;
}

export interface ReadinessCheck {
  key: string;
  label: string;
  status: "pass" | "warning" | "fail" | "unknown" | "not_applicable";
  detail: string;
  blocking: boolean;
  acknowledged: boolean;
}

export interface Readiness {
  ready: boolean;
  blocking: string[];
  warnings: string[];
  checks: ReadinessCheck[];
}

export interface Quota {
  per_minute_used: number;
  per_minute_limit: number;
  rolling_24h_used: number;
  rolling_24h_limit: number;
  in_flight: number;
  max_in_flight: number;
}

export interface Capabilities {
  send: boolean;
  delivery_events: boolean;
  inbox_read: boolean;
  oauth: boolean;
  notes: string[];
}

export interface Account {
  id: string;
  provider: string;
  transport: "gmail_api" | "microsoft_graph" | "smtp";
  mailbox_email: string;
  display_name: string | null;
  sending_domain: string | null;
  state: AccountState;
  connection_tested_at: string | null;
  last_error_code: string | null;
  last_error_summary: string | null;
  provider_metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  policy: Policy | null;
  credentials: {
    configured: boolean;
    backend: string;
    credential_version: number;
    granted_scopes: string[];
    access_token_expires_at: string | null;
    last_refresh_at: string | null;
    last_validated_at: string | null;
    updated_at: string | null;
  } | null;
  capabilities: Capabilities | null;
  readiness?: Readiness;
  quota?: Quota;
  auth_checks: Record<string, string>;
  credential_configured?: boolean;
}

export interface MessageSummary {
  id: string;
  account_id: string;
  kind: "transactional" | "diagnostic_test";
  status: MessageStatus;
  to: string;
  from: string;
  reply_to: string | null;
  subject: string;
  scheduled_at: string | null;
  expires_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  attempt_count: number;
  provider_message_id: string | null;
  submitted_at: string | null;
  delivery_evidence_at: string | null;
  next_attempt_at: string | null;
  final_error_code: string | null;
  final_error_summary: string | null;
  idempotency_key: string | null;
}

export interface DeliveryAttempt {
  id: string;
  attempt_number: number;
  provider: string;
  outcome: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  http_status: number | null;
  smtp_code: number | null;
  enhanced_smtp_code: string | null;
  provider_message_id: string | null;
  retry_after_seconds: number | null;
  diagnostic_code: string | null;
  sanitized_detail: string | null;
  response_metadata: Record<string, unknown>;
}

export interface ProviderEvent {
  id: string;
  message_id: string | null;
  provider: string;
  provider_event_id: string | null;
  event_type: string;
  event_at: string | null;
  received_at: string | null;
  recipient_email: string | null;
  diagnostic_code: string | null;
  sanitized_detail: string | null;
  metadata: Record<string, unknown>;
}

export interface DiagnosticReport {
  id: string;
  engine: string;
  summary: string;
  likely_causes: string[];
  recommended_actions: string[];
  confidence: "low" | "medium" | "high";
  needs_operator_action: boolean;
  created_at: string | null;
  source_attempt_id: string | null;
}

export interface MessageDetail extends MessageSummary {
  text_body: string | null;
  html_body: string | null;
  template_key: string | null;
  template_version: number | null;
  request_fingerprint: string | null;
  lease_owner: string | null;
  lease_expires_at: string | null;
  attempts: DeliveryAttempt[];
  events: ProviderEvent[];
  diagnostics: DiagnosticReport[];
}

export interface TestResult {
  id: string;
  allowed_recipient_id: string;
  message_id: string | null;
  status:
    | "pending"
    | "submitted"
    | "observed_received"
    | "auth_pass"
    | "warning"
    | "failed"
    | "timed_out";
  observed_at: string | null;
  observed_inbox: string | null;
  authentication_results: {
    note?: string;
    raw?: string;
    from_domain?: string | null;
    alignment?: Record<string, string>;
    mechanisms?: Record<string, { result: string; properties: Record<string, string> }>;
  };
  latency_ms: number | null;
  result_detail: string | null;
}

export interface TestRun {
  id: string;
  account_id: string;
  status: "queued" | "running" | "completed" | "completed_with_errors" | "failed" | "cancelled";
  initiated_by: string;
  test_profile: string;
  started_at: string | null;
  completed_at: string | null;
  created_at: string | null;
  summary: Record<string, unknown>;
  results: TestResult[];
}

export interface TestRecipient {
  id: string;
  recipient_email: string;
  label: string | null;
  verification_method: string;
  verified_at: string | null;
  is_enabled: boolean;
  created_at: string | null;
}

export interface DomainCheck {
  id: string;
  account_id: string | null;
  domain: string;
  check_type: string;
  selector: string | null;
  status: string;
  observed_value: string | null;
  evidence: Record<string, unknown>;
  checked_at: string | null;
  expires_at: string | null;
}

export interface SystemStatus {
  version: string;
  app_env: string;
  now: string;
  database: string;
  secret_store: string;
  secret_backend: string;
  global_sending_enabled: boolean;
  backlog: {
    queued: number;
    sending: number;
    needs_review: number;
    oldest_due: string | null;
    expired_leases: number;
  };
  providers: Array<Record<string, unknown>>;
  llm_enabled: boolean;
  retention: { body_days: number; event_days: number; audit_days: number };
  counts: { active_suppressions: number; test_recipients: number };
  limits: { send_rate_limit_per_minute: number; max_message_bytes: number };
}

export interface AccountSummary {
  id: string;
  provider: string;
  transport: string;
  mailbox_email: string;
  display_name: string | null;
  sending_domain: string | null;
  state: AccountState;
  connection_tested_at: string | null;
  last_error_code: string | null;
  last_error_summary: string | null;
  mode: AccountMode | null;
  production_enabled: boolean;
  circuit: {
    state: "closed" | "open" | "half_open" | null;
    open_until: string | null;
    reason: string | null;
  };
  quota: Quota | null;
  readiness: Readiness | null;
  auth_checks: Record<string, string>;
  window_counts: Record<string, number>;
  lifetime_counts: Record<string, number>;
}

export interface SafetyEventSummary {
  id: string;
  severity: "info" | "warning" | "critical";
  event_type: string;
  action_taken: string;
  occurred_at: string | null;
  account_id: string | null;
  details: Record<string, unknown>;
}

export interface ReportSummary {
  generated_at: string;
  period_days: number;
  global_sending_enabled: boolean;
  backlog: SystemStatus["backlog"];
  message_counts: Record<string, number>;
  attempt_outcomes: Record<string, number>;
  provider_events: Record<string, number>;
  delivery_truth: {
    provider_accepted: number;
    delivery_evidence: number;
    hard_bounce_or_complaint: number;
    awaiting_evidence: number;
    note: string;
  };
  open_circuits: Array<{
    account_id: string;
    state: string;
    reason: string | null;
    open_until: string | null;
  }>;
  recent_safety_events: SafetyEventSummary[];
  accounts: AccountSummary[];
}

export interface AuditEntry {
  id: string;
  actor: string;
  action: string;
  target_type: string | null;
  target_id: string | null;
  result: string;
  request_id: string | null;
  source_ip: string | null;
  details: Record<string, unknown>;
  created_at: string | null;
}

export interface SafetyEvent {
  id: string;
  account_id: string | null;
  message_id: string | null;
  severity: "info" | "warning" | "critical";
  event_type: string;
  action_taken: string;
  details: Record<string, unknown>;
  occurred_at: string | null;
  acknowledged_at: string | null;
}

export interface Suppression {
  id: string;
  recipient_email: string;
  reason: string;
  source_account_id: string | null;
  source_message_id: string | null;
  evidence_event_id: string | null;
  created_at: string | null;
  removed_at: string | null;
  removed_by: string | null;
  removal_reason: string | null;
}
