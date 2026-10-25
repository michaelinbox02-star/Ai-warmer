# Private AI Email Deliverability Platform — Technical Specification

**Version:** 1.0  
**Date:** 2026-10-25  
**Deployment:** Single-user/private  
**Initial scale:** 1–5 mailboxes  
**Primary workload:** Transactional emails and notifications  
**Scope:** Authenticated sending, legitimate controlled tests, monitoring, diagnostics, and automatic sending safeguards.

## 1. Executive summary

Build a private web application that connects to up to five email accounts and provides a unified control plane for authenticated transactional sending and deliverability diagnostics. It is not a mechanism for manufacturing engagement or bypassing spam controls. The system should use actual transactional traffic for production and send diagnostic tests only to mailboxes the operator controls or is authorized to test.

The app should make four separate states visible:

1. **Connection works:** the application can authenticate to the provider.
2. **Provider accepted:** the provider accepted the message submission.
3. **Delivery evidence:** a provider event or a controlled recipient mailbox shows delivery or a bounce.
4. **Inbox placement:** verified only through controlled seed-mailbox checks or provider data that actually measures it; never inferred from SMTP `250 OK` alone.

There is no promise of a particular reputation score, inbox placement, or immunity from spam classification. Sender reputation depends on authentication, recipient expectations, actual message traffic, complaints, bounces, provider policies, and other signals.

## 2. Product goals and non-goals

### Goals

- Add, test, pause, reauthorize, and remove 1–5 sender accounts.
- Support Google/Gmail, Microsoft 365/Outlook, and generic authenticated SMTP.
- Check sender-domain SPF, DKIM, and DMARC configuration and preserve dated results.
- Send transactional messages from an internal UI or authenticated local API.
- Use durable queueing, idempotency, bounded retries, rate limits, and a global stop switch.
- Run controlled diagnostic tests to explicitly authorized addresses.
- Record provider responses, delivery events, and sanitized errors.
- Provide rules-based delivery safeguards and optional AI-assisted error explanations.
- Keep credentials out of ordinary application tables, logs, prompts, and browser code.

### Non-goals for version 1

- Public multi-tenant SaaS, shared-account farms, or customer onboarding.
- Generating synthetic conversations or artificial opens/replies to manipulate reputation.
- Bypassing rate limits, account restrictions, spam filters, or provider enforcement.
- Guaranteeing inbox placement or provider reputation outcomes.
- Bulk marketing/cold outreach tooling.
- Arbitrary code execution in email templates or attachments.

## 3. Recommended architecture

```text
                   ┌─────────────────────────────┐
                   │ Private Web UI (Next.js)     │
                   │ Accounts / Tests / Reports   │
                   └──────────────┬──────────────┘
                                  │ HTTPS / local VPN
                   ┌──────────────▼──────────────┐
                   │ FastAPI application         │
                   │ Auth / API / policy checks   │
                   └──────┬─────────────┬───────┘
                          │             │
                ┌─────────▼──────┐  ┌───▼────────────────┐
                │ PostgreSQL     │  │ Secret store       │
                │ jobs + state   │  │ OAuth tokens/keys  │
                └─────────┬──────┘  └────────────────────┘
                          │
                   ┌──────▼───────────────┐
                   │ Sender worker         │
                   │ one active send/account│
                   └───┬────────┬─────────┘
                       │        │
            ┌──────────▼─┐  ┌───▼────────────┐   ┌────────────────┐
            │ Gmail API  │  │ Microsoft Graph│   │ Custom SMTP    │
            └────────────┘  └────────────────┘   └────────────────┘
                       │        │                    │
                       └────────┴──────────┬─────────┘
                                           │
                                  ┌────────▼─────────┐
                                  │ Event intake and │
                                  │ diagnostics      │
                                  └──────────────────┘
```

### Components

- **Web UI:** Next.js + TypeScript. A private operator console; no public registration.
- **API:** Python + FastAPI. Validates payloads, enforces policies, queues messages, manages provider OAuth callbacks, and presents reports.
- **Worker:** a separate process from the API, using the same codebase. It leases due jobs from PostgreSQL, serializes sends per account, reserves quota, invokes the provider adapter, and stores outcomes.
- **Database:** PostgreSQL 16+ (UUIDs, JSONB, partial indexes, row locks). A durable database queue is sufficient at this scale; Redis is not needed for MVP.
- **Secrets:** OS keyring or a managed secrets service. For Docker, use mounted Docker secrets only for bootstrap secrets; do not store live refresh tokens in environment variables. The database stores secret references, not raw credentials.
- **DNS checks:** a DNS resolver library such as `dnspython`; results include a timestamp and supporting evidence.
- **AI diagnostics:** optional LLM call that receives sanitized provider codes, status categories, and metrics—not OAuth tokens, SMTP credentials, or message bodies by default.

### Why no Redis initially?

For 1–5 accounts and modest transactional traffic, PostgreSQL is a suitable durable queue using `FOR UPDATE SKIP LOCKED`, job leases, retries, and an outbox pattern. Add a dedicated queue only when measured throughput or operations justify it.

## 4. Provider integration design

Create a provider interface so the sending controller does not know which protocol is being used.

```python
class EmailProvider:
    async def validate_connection(self, account_id: str) -> ConnectionTestResult: ...
    async def send(self, message: OutboundMessage) -> ProviderSubmissionResult: ...
    async def refresh_credentials(self, account_id: str) -> CredentialRefreshResult: ...
    async def fetch_events(self, account_id: str, cursor: str | None) -> ProviderEventBatch: ...
```

`fetch_events` can be unsupported by a provider adapter in the first release; unsupported capabilities must be shown explicitly rather than guessed.

### Google / Gmail

- Prefer Gmail API `users.messages.send` using OAuth 2.0.
- Request `gmail.send` for send-only operation; request mailbox-read permissions only if an enabled feature genuinely needs to read test-mailbox messages or delivery-status notifications.
- Store OAuth refresh tokens in the secret store. Encrypt the secret-store backend and restrict access to the worker/API identities.
- Preserve the Gmail API message ID and thread ID, if returned, as provider metadata.
- Treat an API success as **accepted/submitted**, not proof of inbox placement.

Official docs: [Gmail API send](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send), [create and send messages](https://developers.google.com/workspace/gmail/api/guides/sending).

### Microsoft 365 / Outlook

- Prefer Microsoft Graph `sendMail` with the least-privilege delegated `Mail.Send` permission for a private user-owned mailbox.
- Use SMTP with OAuth/XOAUTH2 only when a real requirement makes SMTP preferable and the tenant/account configuration supports it.
- Do not implement Exchange Online Basic authentication or assume app passwords will work. Microsoft has disabled Basic authentication in Exchange Online; the exact SMTP availability and tenant policies should be checked during connection validation.
- Consider tenant admin consent only when needed; do not request application-wide mailbox permissions for a personal/private tool unless a documented requirement justifies that scope.

Official docs: [Microsoft Graph sendMail](https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0), [OAuth for SMTP/IMAP/POP](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth), [SMTP AUTH settings](https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/authenticated-client-smtp-submission), [Basic authentication deprecation](https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/deprecation-of-basic-authentication-exchange-online).

### Generic SMTP

- Support host, port, sender address, and provider-supported auth scheme.
- Support STARTTLS on submission port 587 and implicit TLS where the provider explicitly supports it, often port 465.
- Certificate verification must remain enabled. No plaintext fallback, no disabling TLS verification, and no silent downgrade.
- OAuth or short-lived provider credentials are preferred. Where a custom provider requires a password, store it only in the secret store and rotate it when needed.
- Validate DNS host resolution, TLS handshake/certificate, authentication, and a controlled test send separately so a failure is easy to diagnose.
- Restrict custom host destinations to public routable IPs and block loopback, link-local, RFC1918/private, and cloud metadata ranges to reduce SSRF risk. Re-resolve/validate destinations at connection time to reduce DNS-rebinding exposure.

### Common provider result

Every send adapter should return:

- `accepted: bool`
- provider and provider message identifier (if any)
- submission timestamp
- provider status / HTTP or SMTP code
- enhanced SMTP status code where applicable
- a sanitized diagnostic code and message
- retryability classification: `transient`, `permanent`, `auth`, `rate_limited`, `unknown`

Never classify every `4xx` as safe to retry indefinitely or every `5xx` as an identical permanent error without inspecting provider-specific semantics. Authentication, policy, quota, recipient, and content failures need separate handling.

## 5. Message lifecycle and transactional semantics

### Message state machine

```text
queued ──► sending ──► submitted ──► delivered (only with evidence)
   │           │             ├────► bounced
   │           │             └────► complained
   │           ├── transient error ──► retry_wait ──► sending
   │           ├── permanent error ──► failed
   │           └── unknown outcome ──► needs_review
   └──► cancelled
```

For providers without downstream delivery events, an accepted message remains `submitted`. Do not display `delivered` based solely on API acceptance or SMTP `250`.

### Idempotency

- Every API-originated send must carry a caller-supplied `Idempotency-Key` or get a generated stable key at the application boundary.
- Enforce unique `(account_id, idempotency_key)` when a key exists.
- Reusing a key with an identical request returns the existing message; reusing it with a different request returns `409 Conflict`.
- Do not automatically re-submit after an ambiguous timeout unless the provider offers a reliable idempotency mechanism or an operator-approved reconciliation path. Otherwise the message may have been accepted before the connection failed.

### Transactional outbox / durable scheduling

The API writes the message and its queue state in one database transaction. A worker leases due messages using `SELECT … FOR UPDATE SKIP LOCKED`, sets a lease expiration, and processes them. On worker crash, expired leases return to the queue. A per-account lock limits one in-flight submission per mailbox by default.

## 6. Diagnostic test and “warm-up” policy

The UI should label this feature **Controlled deliverability tests**, not “reputation guarantee.”

Allowed test behavior:

- Test a mailbox connection and send a small number of identifiable test messages to addresses the operator has explicitly verified and controls/has authorization to test.
- Verify that a recipient mailbox receives the message, read `Authentication-Results` when available, and compare expected sender/domain alignment.
- Measure provider acceptance, time-to-receive in test inboxes, authentication results, bounce/deferral events, and TLS behavior.
- Run tests on demand or on a modest diagnostic schedule.
- Keep test recipients in a verified allowlist and use a clear test subject/header.

Production warm-up policy:

- Do not manufacture engagement or send unsolicited “warm-up conversations.”
- Turn on production sending only after connection and domain checks have passed or any warnings are explicitly acknowledged.
- Increase production traffic according to actual expected transactional demand and provider limits, not a fixed fake-engagement schedule.
- Keep transactional messages on a consistent, truthful sender identity and use relevant message content.

### Readiness gate

`ready_for_production = connection_valid AND TLS_valid_if_SMTP AND sender_identity_verified AND auth_checks_meet_policy AND global_sending_enabled AND account_mode == production`.

The exact authentication policy is configurable because the right setup depends on the domain and provider. For a domain that sends mail, recommend SPF, DKIM and DMARC even when a provider’s minimum requirement is narrower. Gmail guidance says senders must use SPF or DKIM at minimum, and bulk senders have further requirements; it recommends SPF, DKIM, DMARC, alignment and TLS. See [Gmail sender guidelines](https://support.google.com/mail/answer/81126?hl=en).

## 7. Sending safeguards and circuit breakers

### Mandatory controls

1. Global kill switch: stops all future submissions; already accepted messages cannot be recalled.
2. Per-account state: `test_only`, `production`, or `paused`.
3. Manual production enablement per account after readiness checks.
4. Per-account rolling 24-hour cap and per-minute cap, both configurable and set conservatively on first enablement.
5. Serialize sends per account initially.
6. Exponential backoff with jitter for retryable errors; respect provider `Retry-After` or equivalent hints.
7. Max-attempt cap; do not retry permanent recipient, authentication, authorization, policy, or malformed-message errors.
8. Pause on expired/revoked credentials, provider quota errors, repeated TLS failures, or sudden elevated provider errors.
9. Suppression table for hard bounces and complaints; check before queueing and immediately before sending.
10. Audit every state change, manual override, credential reconnection, and kill-switch action.
11. No retries after an ambiguous outcome unless safe reconciliation is available.
12. Alert the operator instead of automatically “rotating” senders to get around a provider limit.

### Suggested initial policy defaults

These are internal safety caps, not claims about any provider’s quotas. Make them configurable and start lower if the account has little legitimate sending history.

- Mode on account creation: `test_only`.
- Production sending: disabled by default.
- Max in-flight sends per account: 1.
- Internal cap: 25 submitted messages per rolling 24 hours per account at first activation; operator may adjust once real transactional needs and provider policies are understood.
- Internal cap: 3 submissions per minute per account at first activation.
- Max retries: 5, with provider-aware backoff.
- Open circuit: on auth failures, explicit quota/abuse restrictions, TLS validation errors, or 5 consecutive transient failures. Some conditions require manual reset.
- Automatic send recovery: do not automatically resume a manually paused account, a complaint-related pause, or an explicit provider restriction.

### Retry schedule

A sample exponential schedule before jitter is 30 seconds, 2 minutes, 10 minutes, 30 minutes, 2 hours. The actual schedule must respect the provider’s retry hints and the message’s expiration/urgency. For one-time security codes, expire the message instead of sending a stale code later.

### Circuit breaker states

- `closed`: sends permitted if all policy checks pass.
- `open`: sends blocked until `open_until` or manual review.
- `half_open`: allow one low-risk probe/test send, not a production burst.

An open circuit should show a human-readable cause and the diagnostic records used to trigger it.

## 8. Domain and delivery diagnostics

### DNS/authentication checks

Store dated results for:

- SPF: record exists, is syntactically valid, and does not appear to exceed DNS lookup limits.
- DKIM: check the configured selector(s); no universal reliable discovery of every selector should be assumed.
- DMARC: record exists and parses; report the policy and alignment settings.
- MX: useful for domain mail-routing diagnostics, but not proof of outbound delivery.
- TLS: SMTP endpoint handshake and certificate verification.
- Sender alignment: compare visible From domain to SPF-authenticated envelope domain and DKIM signing domain when the message is received by a controlled test mailbox.

DNS checks are diagnostics. They cannot by themselves prove the provider is signing every actual message or that a receiver accepts it.

### Delivery telemetry

Track:

- accepted submission count
- transient and permanent failure counts
- SMTP/HTTP status families
- bounce and complaint event counts where available
- time to provider acceptance
- time to observed receipt at controlled test inboxes
- rate-limit and quota events
- auth/connection failures

Do not treat opens as a reliable deliverability metric. Gmail says it does not track open rates for this purpose and cannot verify third-party open-rate accuracy. See [Gmail sender guidelines](https://support.google.com/mail/answer/81126?hl=en).

### Gmail reputation data

Optionally add a link/checklist for Google Postmaster Tools. Its dashboards can show Gmail-focused spam-rate, authentication, reputation and delivery-error data, and apply to mail sent to personal Gmail accounts. It is not universal monitoring for every provider or recipient. See [Set up Postmaster Tools](https://support.google.com/mail/answer/9981691?hl=en).

## 9. Optional AI diagnostic assistant

AI is advisory, not a send authority.

Inputs allowed by default:

- provider name, status family, SMTP enhanced status code, exception category
- sanitized and truncated error text
- aggregate metrics, recent retry count, DNS-check outcomes
- non-sensitive configuration labels

Inputs excluded by default:

- refresh tokens, access tokens, passwords, API keys, authorization headers
- entire message bodies, security codes, attachments, or recipient lists
- raw DSNs that include unrelated recipient data

Output schema:

```json
{
  "summary": "Short explanation",
  "likely_causes": ["Cause A", "Cause B"],
  "recommended_actions": ["Action A"],
  "confidence": "low|medium|high",
  "needs_operator_action": true
}
```

The UI must call this a diagnosis or suggestion. Only deterministic code changes account state, opens a circuit, or schedules a message.

## 10. Security and privacy

- Bind the UI to localhost or a private VPN/reverse proxy. Do not expose directly to the public internet by default.
- Use a single admin identity with MFA/passkey if the chosen auth system supports it. Disable public registration.
- Require HTTPS outside loopback.
- Keep live OAuth tokens/passwords in a secret store. Store only `secret_ref` and metadata in PostgreSQL. If using an encrypted local vault, its master key must be stored separately from the database and backed up carefully.
- Use least-privilege OAuth scopes. Separate send-only functionality from mailbox reading.
- Redact secrets from all logs, exception traces, analytics, and AI prompts.
- Validate custom SMTP endpoints against SSRF; disallow private/internal destinations and unverified TLS.
- Rate limit the local API and protect OAuth `state`/PKCE flows.
- Validate all HTML email content, escape template values, and reject unsafe attachments by policy.
- Limit attachment size and MIME types if attachments are later added.
- Store data only as long as required. Use a configurable retention window for bodies and logs; preserve minimal event metadata for debugging.
- Use database TLS where supported, encrypted backups, and test a restore procedure.
- Record audit entries for credential changes, account mode transitions, global pause/resume, and policy overrides.

## 11. Suggested implementation stack

| Layer | Choice | Rationale |
|---|---|---|
| UI | Next.js + TypeScript | Good fit for a small private dashboard |
| API | Python + FastAPI | Async I/O, provider libraries, type-friendly API |
| Database | PostgreSQL 16+ | Durable scheduling, JSONB diagnostics, row locks |
| ORM/migrations | SQLAlchemy 2 + Alembic | Typed models and controlled schema migration |
| DNS | dnspython | Direct DNS diagnostics |
| HTML/email | Python `email` package or a well-maintained MIME builder | RFC-compliant message construction |
| Secrets | OS keyring / managed secret service | Keep credentials out of relational data |
| Packaging | Docker Compose | Reproducible single-host deployment |
| AI | Optional LLM adapter | Error explanation only; no send authority |

Pin supported dependency versions and update them through automated vulnerability checks. Treat provider APIs and OAuth scopes as versioned integration contracts.

## 12. Core database design

The executable PostgreSQL schema is in [`schema.sql`](schema.sql). It defines these primary records:

| Table | Purpose |
|---|---|
| `email_accounts` | Provider/mailbox identity and connection state |
| `provider_credentials` | Secret-store pointer and token metadata only |
| `send_policies` | Production/test mode, quotas, circuit state |
| `allowed_test_recipients` | Explicitly approved test destinations |
| `email_messages` | Durable transactional/test messages and state |
| `delivery_attempts` | One record per provider submission attempt |
| `quota_reservations` | Race-safe quota reservations before sending |
| `provider_events` | Bounce, complaint, deferral, delivery, and diagnostic events |
| `suppressions` | Hard-bounce and complaint suppression list |
| `domain_auth_checks` | Timestamped SPF/DKIM/DMARC/MX/TLS results |
| `test_runs` | Controlled diagnostic test runs |
| `test_run_results` | Outcome per authorized test recipient |
| `safety_events` | Circuit-breaker and policy-trigger records |
| `audit_log` | Operator actions and security-relevant events |
| `diagnostic_reports` | Optional structured AI/rules-based explanations |

### Data model rules

- All primary IDs are UUIDs.
- Database timestamps are `timestamptz` in UTC.
- `provider_credentials.secret_ref` is a pointer, never the secret itself.
- `email_messages.status = submitted` means the provider accepted the send; `delivered` requires downstream evidence.
- `idempotency_key` is unique per account when supplied.
- Quota reservations are taken before external I/O and consumed only after provider acceptance; transient/permanent failures release the reservation.
- The worker serializes submissions per account and uses a row lock on the account's send policy during reservation.
- `provider_events` must be idempotent where provider event IDs are available.
- The suppression check runs both at queue time and immediately before sending.
- Message bodies and addresses are sensitive data; database-level encryption, application authorization, backup encryption, and retention settings are required.

## 13. Minimal API contract

All endpoints require the private admin session or a local service token. Do not create an anonymous endpoint that can be used to relay email.

| Method and path | Purpose |
|---|---|
| `GET /api/v1/accounts` | List accounts and current states |
| `POST /api/v1/accounts/oauth/{provider}/start` | Begin OAuth/PKCE flow |
| `POST /api/v1/accounts/smtp` | Save custom SMTP configuration; secret goes to vault |
| `POST /api/v1/accounts/{id}/test-connection` | Validate connectivity and auth |
| `POST /api/v1/accounts/{id}/domain-checks` | Run DNS/auth checks |
| `POST /api/v1/accounts/{id}/mode` | Set test-only/production/paused with audit |
| `POST /api/v1/messages` | Queue one transactional message, idempotent |
| `GET /api/v1/messages/{id}` | Return status and safe diagnostics |
| `POST /api/v1/tests` | Run controlled test to allowlisted recipients |
| `POST /api/v1/system/pause` | Engage global kill switch |
| `POST /api/v1/system/resume` | Resume only after explicit operator action |
| `GET /api/v1/reports/summary` | Delivery, failure, quota, and auth summary |

A send request should include `account_id`, recipient, subject, text/HTML body, template/version if used, `expires_at` for time-sensitive notifications, and an idempotency key. Enforce payload size limits and validate sender identity against the connected account.

### Example send request

```json
{
  "account_id": "00000000-0000-0000-0000-000000000001",
  "to": "recipient@example.net",
  "subject": "Your service notification",
  "text_body": "Your requested notification is ready.",
  "html_body": null,
  "expires_at": "2026-10-25T13:00:00Z"
}
```

The UUID and recipient above are illustrative. Require an `Idempotency-Key` HTTP header for clients that can retry requests.

## 14. Observability and operator dashboard

### Home page

- Global state: sending enabled/paused.
- Account cards: provider, mailbox, connection status, mode, most recent submission, recent error, next action.
- Readiness checklist: connection, sender identity, DNS/authentication, TLS (where applicable), policy acknowledgement.
- Summary: queued, submitted, bounced, deferred, failed, and test messages over the selected period.
- Circuit breaker details with reason and timestamp.

### Account detail

Tabs: Overview, Domain checks, Test results, Message log, Policy, Credentials. Never display tokens or passwords again after a secret is saved.

### Alerts

- OAuth revoked/expired.
- TLS certificate or connection failure.
- Provider rate limiting or explicit quota restriction.
- Repeated SMTP 4xx/5xx errors.
- Hard bounce or complaint event.
- SPF/DKIM/DMARC missing or changed.
- Circuit breaker opened.
- Worker backlog or a job lease repeatedly expiring.

### Retention

Initial recommendation: message body retention configurable (e.g. 7–30 days); sanitized attempt/event metadata 90 days; audit events 180 days or longer if useful. Make the settings explicit and document that backup copies age out on their own schedule.

## 15. Testing and acceptance criteria

The MVP is not complete until the following have test coverage:

1. OAuth state/PKCE validation, redirect URI validation, token refresh, and revoked-token behavior.
2. Secret values never appear in database dumps, browser responses, logs, or AI prompts.
3. SMTP requires TLS with certificate validation; invalid certificates fail closed.
4. Custom SMTP host validation blocks loopback, private, link-local, and metadata endpoints.
5. Repeating the same idempotency key cannot create a duplicate message.
6. A crashed worker's expired lease is recoverable.
7. Quota reservations prevent concurrent overrun; failed submissions release reservations.
8. Provider acceptance only sets `submitted`, not `delivered`.
9. Temporary provider errors retry with backoff; permanent/auth/policy errors do not retry indefinitely.
10. Global pause blocks new submissions and the UI explains that already accepted messages cannot be recalled.
11. Test sends are blocked unless each destination is allowlisted and verified.
12. Hard-bounced/complaint recipients are suppressed and cannot be sent to from any connected account without explicit, audited removal.
13. Circuit breaker opens for configured conditions and does not silently resume after a manual or provider-enforcement pause.
14. Authentication checks preserve timestamp, observed evidence, and parsed result.
15. Restore from encrypted backup is tested.

## 16. Implementation plan

### Phase A — Foundation

- Docker Compose, private access/auth, PostgreSQL, Alembic, secret-store adapter.
- Accounts and provider credential lifecycle.
- Connection tests and provider interface.

### Phase B — Safe sending

- Message table and durable worker.
- Idempotency, quota reservations, per-account serialization, retry policy, suppression checks.
- Global and per-account pause, audit log.

### Phase C — Diagnostics

- SPF/DKIM/DMARC/MX checks and TLS probe.
- Controlled test allowlist, test-run reports, provider event intake where supported.
- Dashboard and alert rules.

### Phase D — AI assistance

- Sanitized provider-error summarizer.
- Recommendations stored as diagnostic reports.
- Explicitly keep AI outside the sending authorization path.

### Definition of done

The operator can connect at least one supported account, pass a connection/readiness check, run a test to a verified mailbox, queue a transactional message with idempotency, see its true submission state, inspect diagnostics, pause all sending immediately, and confirm that credentials never appear in application logs or the database.

## 17. Official references

- [Gmail API: send message](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send)
- [Gmail API: create and send messages](https://developers.google.com/workspace/gmail/api/guides/sending)
- [Microsoft Graph: sendMail](https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0)
- [Microsoft: OAuth for SMTP/IMAP/POP](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth)
- [Microsoft: authenticated client SMTP submission](https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/authenticated-client-smtp-submission)
- [Microsoft: Exchange Online Basic authentication deprecation](https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/deprecation-of-basic-authentication-exchange-online)
- [Gmail sender guidelines](https://support.google.com/mail/answer/81126?hl=en)
- [Google Postmaster Tools setup](https://support.google.com/mail/answer/9981691?hl=en)
- [Google Workspace Gmail sending limits](https://knowledge.workspace.google.com/admin/gmail/gmail-sending-limits-in-google-workspace)

Provider rules and quotas can change. Re-check the official provider documentation as part of release validation rather than hard-coding published quotas as permanent constants.
