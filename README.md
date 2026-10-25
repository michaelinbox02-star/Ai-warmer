# Deliverability Console

A private, single-operator email deliverability and controlled-testing platform for
1–5 operator-owned mailboxes. It sends provider-accepted mail, runs diagnostic
tests only to verified allowlisted recipients, and records delivery evidence
without ever pretending that provider acceptance means inbox placement.

Built from [`schema.sql`](schema.sql) and
[`technical-specification.md`](technical-specification.md).

**Version:** 1.0.0

---

## The four-state safety model

These are separate facts with separate sources. The product never collapses
them into a single "delivered" badge.

| Fact | How it is established | Stored as |
| --- | --- | --- |
| Connection works | Operator-run TLS/authentication probe | `connection_valid`, connection test result |
| Provider accepted the message | API response / SMTP `250` / Graph `202` | `submitted` |
| Delivery evidence exists | Provider event relay (DSN, delivery webhook, complaint) | `delivered`, `bounced`, `complained` |
| Inbox placement observed | Operator pastes raw headers from the actual recipient mailbox | Manual observation + parsed authentication results |

Provider acceptance is **never** displayed as delivery. Gmail and Microsoft Graph
do not normally expose downstream delivery events for `gmail.send` /
`Mail.Send`, so accepted messages remain `submitted` until an event source is
connected. SMTP has no reliable per-message delivery receipt in this design.

There is no inbox-warming engagement, no fake opens/replies, no spam-filter
bypass, and no sending to purchased lists. Diagnostic sends are restricted to
recipients the operator has added and verified in the allowlist.

---

## Architecture

```text
Browser
  │  same-origin requests
  ▼
Next.js operator console (:3000)
  │  /api/v1/* rewrite
  ▼
FastAPI API (:8000) ──────────────► PostgreSQL 16
  │  secrets decrypted in memory          (state, queue, audit, events)
  ▼
Provider adapters: Gmail API · Microsoft Graph · SMTP (STARTTLS/TLS)
  ▲
  │  separate process, no inbound ports
Sender worker (python -m app.worker)
  │
  └─ encrypted local secret vault (named volume, Fernet)
```

- **API** — authentication, account lifecycle, readiness gate, idempotent
  queueing, reports, event intake, diagnostics.
- **Worker** — leases queued messages, reserves quota, calls the provider
  outside any database transaction, and finalizes honestly.
- **Web** — operator console; static/server components only. All calls go
  through the Next.js rewrite, so the browser never talks cross-origin to the
  API.
- **PostgreSQL** — durable queue, quota reservations, attempts, audit trail,
  provider events.

### Repository layout

```text
schema.sql                     canonical PostgreSQL DDL
technical-specification.md     product and safety requirements
docker-compose.yml             db, migrate, api, worker, web
.env.example                   every supported environment variable
api/
  app/
    main.py                    FastAPI app, middleware, exception envelope
    config.py                  pydantic-settings configuration
    models.py                  SQLAlchemy models
    routers/                   auth, accounts, messages, tests, reports, system, events
    services/                  queue, sender, finalize, policy, events, secrets, ...
    providers/                 SMTP, Gmail, Graph, OAuth, SSRF guard
    core/                      readiness, retry, headers, redaction, MIME, idempotency
  alembic/                     0001 baseline, 0002 oauth flows, 0003 attempt offset
  tests/                       unit and ASGI tests (no database required)
web/
  app/(console)/               accounts, messages, tests, reports, suppressions, settings
  components/                  shared UI primitives
scripts/
  init-env.ps1                 generate a complete .env with secrets
  dev.ps1                      local dependency bootstrap
  migrate.ps1                  alembic upgrade head
  backup.py                    encrypted pg_dump + vault archive
  restore.py                   encrypted restore
```

---

## Quick start with Docker Compose

### Prerequisites

- Docker Desktop (Linux containers) with Compose v2
- PowerShell 7 or Windows PowerShell 5.1 for `scripts/init-env.ps1`
- Ports `3000` and `8000` free on loopback

Both ports are published to `127.0.0.1` only. Do not expose them directly to the
internet; put a TLS reverse proxy in front and set `PUBLIC_BASE_URL` to the
HTTPS URL.

### 1. Generate `.env`

```powershell
powershell -ExecutionPolicy Bypass -File scripts\init-env.ps1
```

The script prompts for the single operator password (minimum 12 characters),
then writes `.env` with:

- a URL-safe PostgreSQL password,
- a PBKDF2 password hash (the plaintext password is never written),
- `SESSION_SECRET`, `API_TOKEN`, `EVENT_INGEST_TOKEN`,
- a Fernet `VAULT_MASTER_KEY`.

It prints nothing secret. Store the operator password and `VAULT_MASTER_KEY` in
a password manager. **Losing the vault key means every stored provider token
must be reconnected.**

If you prefer to generate values manually:

```powershell
cd api
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m app.cli gen-key        # VAULT_MASTER_KEY
.\.venv\Scripts\python.exe -m app.cli gen-token      # SESSION_SECRET / API_TOKEN / event token
.\.venv\Scripts\python.exe -m app.cli hash-password  # ADMIN_PASSWORD_HASH
Copy-Item ..\.env.example ..\.env
```

Then fill in every secret in `.env`.

### 2. Start

```powershell
docker compose up -d --build
docker compose ps
```

Startup order is enforced:

1. `db` becomes healthy (`pg_isready`).
2. `migrate` runs `alembic upgrade head` once and exits successfully.
3. `api` and `worker` start against the migrated database.
4. `web` starts once the API health check passes.

### 3. Sign in

Open <http://localhost:3000>. Use the operator password from step 1.

### 4. Connect the first mailbox

Use **Accounts → Add account**:

- **Google / Google Workspace** — starts OAuth with the `gmail.send` scope.
- **Microsoft 365 / Outlook.com** — starts OAuth with delegated `Mail.Send`.
- **Custom SMTP** — host, port, username, password, and `starttls` or `tls`.

OAuth providers must be configured first (see [Provider setup](#provider-setup)).
An account is created disabled and in `test_only` mode; nothing can be sent
until the readiness gate is satisfied and the operator explicitly enables
production.

---

## Local development

You need PostgreSQL 16 reachable from the host. The API and worker do not run
without it.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
Copy-Item .env.example .env
```

For local development set `APP_ENV=local`, use a local `DATABASE_URL`, and either
set an operator password or let the API start without sign-in for inspection.
Run migrations and start the three processes:

```powershell
# terminal 1 — API
cd api
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000 --reload

# terminal 2 — worker
cd api
.\.venv\Scripts\python.exe -m app.worker

# terminal 3 — UI
cd web
npm run dev
```

`web/next.config.ts` rewrites `/api/v1/:path*` to `API_INTERNAL_URL`
(default `http://localhost:8000`). The value is baked at build time; for
Compose it is `http://api:8000`.

### Useful commands

| Task | Command |
| --- | --- |
| Backend tests | `cd api; .\.venv\Scripts\python.exe -m pytest -q` |
| Compile check | `cd api; .\.venv\Scripts\python.exe -m compileall -q app` |
| Web typecheck | `cd web; npm run typecheck` |
| Web production build | `cd web; npm run build` |
| Print effective config | `cd api; .\.venv\Scripts\python.exe -m app.cli config` |
| Prune orphaned vault files | `cd api; .\.venv\Scripts\python.exe -m app.cli prune-secrets` (dry run) |

---

## Configuration

All settings are environment variables; see [`.env.example`](.env.example) for
the annotated list. The most important ones:

| Variable | Purpose |
| --- | --- |
| `APP_ENV` | `local` or `production`. Production enforces strong secrets. |
| `PUBLIC_BASE_URL` | Operator UI origin; used for OAuth redirects and CSRF/origin checks. |
| `DATABASE_URL` | `postgresql+asyncpg://...` for the API/worker. Compose overrides the host. |
| `POSTGRES_PASSWORD` | Compose-only password for the `db` service. Use URL-safe characters. |
| `ADMIN_PASSWORD_HASH` | Preferred operator credential (PBKDF2-SHA256). |
| `ADMIN_PASSWORD` | Local-only plaintext alternative; production requires ≥12 chars. |
| `SESSION_SECRET` | HMAC key for signed session cookies; required in production. |
| `API_TOKEN` | Optional bearer token for local automation. Empty disables it. |
| `EVENT_INGEST_TOKEN` | Token for the local provider-event relay. Empty disables intake. |
| `SECRET_BACKEND` | `encrypted_local_vault` (default), `docker_secret`, or `os_keyring`. |
| `VAULT_DIR` | Vault directory; `/data/secrets` in Compose. |
| `VAULT_MASTER_KEY` | Fernet key required in production. Back it up separately. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Enable Google OAuth when both are set. |
| `MICROSOFT_CLIENT_ID` / `MICROSOFT_CLIENT_SECRET` | Enable Microsoft OAuth. |
| `MICROSOFT_TENANT` | `common`, `organizations`, `consumers`, or a tenant id. |
| `LLM_ENABLED` | Optional advisory diagnostics; never sends or changes state. |
| `RETENTION_*_DAYS` | Body/event/audit retention policy used by the cleanup endpoint. |

Generate a PostgreSQL password with URL-safe characters only (`A–Z a–z 0–9 - _`);
otherwise the password must be percent-encoded inside `DATABASE_URL`.

### Secret backends

| Backend | Use case | Notes |
| --- | --- | --- |
| `encrypted_local_vault` | Default single-host deployment | One Fernet-encrypted file per credential; atomic writes; 0600 where supported. |
| `docker_secret` | Docker/Swarm secrets mounted as files | Read-only; the operator provisions each credential file. |
| `os_keyring` | Host-local desktop experiment | Requires a working OS keyring; not recommended in containers. |

Secrets never enter PostgreSQL. The database stores only `vault://<random>`
references and non-secret metadata.

---

## Provider setup

### Google / Google Workspace

1. In Google Cloud Console create an OAuth client of type **Web application**.
2. Add the authorized redirect URI:
   `http://localhost:3000/api/v1/accounts/oauth/google_gmail/callback`
   (and the same with `google_workspace` if you use that provider name).
   For a real deployment use your `PUBLIC_BASE_URL`.
3. Enable the **Gmail API**.
4. Configure the OAuth consent screen. For private use, keep the app in
   **Testing** and add the mailbox as a test user, or use an Internal app in a
   Workspace organization. `gmail.send` is a restricted scope; public
   verification is outside this project.
5. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, restart the API, then use
   **Accounts → Add account → Google**.

Requested scopes: `openid`, `email`,
`https://www.googleapis.com/auth/gmail.send`. No read scope is requested.

### Microsoft 365 / Outlook.com

1. In Microsoft Entra admin center create an **App registration**.
2. Add a **Web** redirect URI:
   `http://localhost:3000/api/v1/accounts/oauth/microsoft_365/callback`
   (and `outlook_com` if used).
3. Add delegated Microsoft Graph permission **Mail.Send**. Grant admin consent
   if your tenant requires it.
4. Create a client secret and set `MICROSOFT_CLIENT_ID`,
   `MICROSOFT_CLIENT_SECRET`, and optionally `MICROSOFT_TENANT`.
5. Use **Accounts → Add account → Microsoft**.

Requested scopes: `openid`, `email`, `offline_access`,
`https://graph.microsoft.com/Mail.Send`. Graph returns `202 Accepted` without a
message id; the platform records `submitted` and stores the client request id.

### Custom SMTP

- Host and port are validated against SSRF rules: loopback, private, link-local,
  multicast, and metadata addresses are rejected. Use a public SMTP relay.
- Security must be `starttls` or implicit `tls`. There is **no plaintext
  fallback**. Certificate validation is mandatory and cannot be disabled.
- Use an app-specific password where the provider requires one. The password is
  written directly to the encrypted vault, never to the database or logs.
- Standard ports: `587` (STARTTLS), `465` (TLS).

The SMTP adapter records the `Message-ID` header as the provider message id
because SMTP itself returns no id. A `250` response is acceptance only.

---

## Operator workflow

1. **Add the account** and complete provider authorization/SMTP credentials.
2. **Test connection** (`Accounts → Overview`). This verifies DNS, TLS with
   certificate validation, authentication, and (for OAuth) token refresh.
3. **Run domain checks** for SPF, DKIM, DMARC, and MX. The readiness gate uses
   the configured per-domain policy (SPF and DKIM required by default; DMARC
   strongly recommended; MX informational).
4. **Verify sender identity** and acknowledge any non-blocking warnings.
   Production cannot be enabled while a blocking check fails.
5. **Add diagnostic recipients** (`Tests → Allowlist`) and verify them out of
   band. Test sends are refused for anyone not on the allowlist.
6. **Enable production** with the required acknowledgements. The UI states that
   already accepted messages cannot be recalled.
7. **Send transactional mail** (`Messages → New`). An `Idempotency-Key` header
   is mandatory. Repeating the same key with the same payload returns the
   existing message; a different payload returns `409`.
8. **Watch the message lifecycle**: `queued → sending → submitted`, then
   `delivered`/`bounced`/`complained` only from provider evidence. Ambiguous
   timeouts become `needs_review` and are never retried automatically.
9. **Record inbox observations** (`Tests → run → result`): paste the raw
   `Authentication-Results` headers from the recipient mailbox. The parser
   extracts SPF/DKIM/DMARC alignment and stores the observation separately from
   delivery evidence.
10. **Use the kill switch** (`Reports` or `Settings`): global pause stops new
    submissions immediately. In-flight and already accepted messages cannot be
    recalled.

### Quotas, retries, and circuits

- Defaults: 3 messages/minute, 25 per rolling 24 hours, 5 attempts, 1 in flight.
  These are deliberately conservative and editable per account with an audited
  reason.
- A quota slot is reserved under a row lock **before** provider I/O. Confirmed
  rejections release it; ambiguous outcomes consume it, so uncertainty can
  never increase the effective send rate.
- Transient failures retry with jittered backoff (30s, 2m, 10m, 30m, 2h).
  Authentication, permanent, and policy failures do not retry indefinitely.
- Circuits open on credential rejection, quota/rate restriction, TLS failure,
  complaint, and repeated transient failures. Some require an audited manual
  reset; transient circuits half-open automatically.
- `needs_review` means the provider may have accepted the message. Requeueing
  requires an explicit confirmation of the duplicate-delivery risk. Attempt
  numbering is preserved across requeues.

---

## Provider events and suppression

The event endpoint is intentionally not a public webhook:

```text
POST /api/v1/events/provider
X-Event-Token: <EVENT_INGEST_TOKEN>
Content-Type: application/json
```

Example:

```json
{
  "account_id": "00000000-0000-0000-0000-000000000000",
  "event_type": "bounce",
  "event_at": "2026-10-25T12:00:00Z",
  "provider_event_id": "relay-123",
  "message_id": "00000000-0000-0000-0000-000000000001",
  "recipient_email": "recipient@example.net",
  "bounce_type": "hard",
  "diagnostic_code": "5.1.1",
  "detail": "Mailbox does not exist"
}
```

`provider_event_id` makes ingestion idempotent. A match by `message_id` or
`provider_message_id` may change message state. A match by recipient only is
recorded, can suppress the recipient, but **does not** rewrite message status —
the relay operator must supply a stable id for state transitions.

Hard bounces and complaints create a global suppression entry. Suppressed
recipients are blocked from every connected account. Removal requires an
audited operator action with a reason.

---

## Security model

- **Single operator.** Public registration does not exist. Session cookies are
  signed, `HttpOnly`, `SameSite=Lax`, and `Secure` when
  `PUBLIC_BASE_URL` is HTTPS. State-changing browser requests require a
  double-submit CSRF token. Login is rate-limited per client key.
- **Automation.** Optional `Authorization: Bearer <API_TOKEN>` for local
  scripts. Empty token disables this path.
- **Secrets.** Provider tokens/passwords live only in the configured secret
  store. They are decrypted into memory for a single provider call. API
  responses expose only `configured`, version, scopes, and timestamps.
  Redaction removes tokens, passwords, cookies, and credential-bearing URLs
  from diagnostics, logs, and optional AI prompts.
- **Transport.** SMTP requires verified TLS ≥1.2 with hostname checking and no
  plaintext fallback. Custom hosts are resolved and checked against private,
  loopback, link-local, and metadata ranges before connecting.
- **Database.** SQLAlchemy parameterizes queries. Credentials are never stored
  in the database. Audit and safety events capture overrides, mode changes,
  circuit resets, suppression removal, pauses, and requeues.
- **AI diagnostics.** Optional and advisory. The feature is disabled by
  default, receives only redacted text, cannot send mail, and cannot change
  policy or message state.
- **Retention.** Bodies, events, and audit records have configurable retention.
  Cleanup is an explicit operator action (`POST /api/v1/system/retention`).

Report security issues privately to the repository owner. This is a private
single-tenant tool, not a multi-tenant SaaS.

---

## Backup and restore

Back up **both** the database and the encrypted vault. The database contains
`vault://` references; the vault files contain the encrypted credentials; the
master key is a third, separate item.

### Create an encrypted backup

```powershell
cd api
.\.venv\Scripts\python.exe ..\scripts\backup.py
```

Outputs to `backups/`:

- `deliverability-<timestamp>.dump.enc` — encrypted custom-format `pg_dump`
- `vault-<timestamp>.tar.enc` — encrypted archive of `/data/secrets`

The passphrase is read from `DLP_BACKUP_PASSPHRASE` or prompted for. The
plaintext dump exists only in a temporary file that is deleted in a `finally`
block. The backup format uses Fernet (AES-128-CBC + HMAC) with a PBKDF2-SHA256
key derived from the passphrase (390,000 iterations, random 16-byte salt).

Pass `--skip-vault` only if you have another way to back up provider tokens.

### Restore

```powershell
docker compose stop api worker     # keep db running
cd api
.\.venv\Scripts\python.exe ..\scripts\restore.py `
  --input ..\backups\deliverability-....dump.enc `
  --vault ..\backups\vault-....tar.enc
docker compose start api worker
```

`pg_restore --clean --if-exists` is destructive; the script requires typing
`RESTORE` unless `--yes` is passed. Restore the vault archive only with the
matching `VAULT_MASTER_KEY`. Then re-run connection tests and readiness checks
before enabling production.

Store the backup passphrase and `VAULT_MASTER_KEY` separately from the backup
files (password manager, sealed envelope, or secrets manager). An encrypted
backup without its passphrase is unrecoverable by design.

---

## Testing

Backend unit/ASGI tests do not need PostgreSQL:

```powershell
cd api
.\.venv\Scripts\python.exe -m pytest -q
```

They cover:

- readiness gate, retry schedule, circuit decisions, suppression blocking;
- MIME construction and header-injection rejection;
- authentication results parsing and alignment;
- idempotency key validation and fingerprint comparison;
- redaction of tokens, passwords, cookies, and URLs;
- SSRF address classification;
- secret vault encryption, access control, tamper detection, and invalid refs;
- SMTP TLS context requirements;
- provider error classification;
- Gmail/Graph submission outcomes with mocked HTTP, including acceptance,
  message id behavior, rate limiting, transport failure, and missing refresh
  token;
- OAuth refresh/exchange, PKCE verifier forwarding, revoked tokens, and
  configuration errors;
- encrypted backup format round-trip, wrong passphrase, tampering, and file
  helpers;
- API health, security headers, stable error envelope, and the
  authentication wall.

The web console is validated with:

```powershell
cd web
npm run typecheck
npm run build
```

### What is not covered here

This repository was built in an environment **without Docker and without
PostgreSQL**, so the following were not executed during the build:

- Alembic migrations against a live database;
- database-backed routes and worker flows (leasing, crash recovery, quota
  reservations, readiness persistence, suppression, event reactions);
- a real provider send through Gmail, Microsoft Graph, or SMTP;
- live OAuth redirect/state/PKCE round-trip (helpers are unit-tested with
  mocked token endpoints);
- `pg_dump`/`pg_restore` and vault archive restore;
- end-to-end browser flows against a running API.

The code paths exist and are covered by pure/unit tests where possible, but do
not treat this as production-verified until you run the integration checklist
below on a real host.

### Integration checklist (required before production)

1. `docker compose up -d --build`; confirm `migrate` exits 0 and `/readyz`
   reports `database: ok`, `secret_store: ok`.
2. Connect one mailbox; run connection test and domain checks; confirm
   production is blocked while a required check fails.
3. Send one diagnostic test to an allowlisted mailbox; confirm status stops at
   `submitted` until evidence arrives.
4. Submit a webhook `delivery` event with the same `message_id`; confirm
   `delivered`. Repeat the event and confirm idempotency.
5. Submit a hard-bounce event; confirm suppression blocks a send from another
   account and removal is audited.
6. Stop the worker mid-send (or expire a lease) and confirm the message becomes
   `needs_review`, not automatically resent.
7. Lower the per-minute cap and confirm concurrent sends cannot overrun it.
8. Trigger an authentication failure and confirm the circuit opens and requires
   an audited reset.
9. Use global pause and confirm queued messages do not start; resume and verify.
10. Run backup, wipe a disposable database, restore database + vault, and verify
    accounts and a test connection.

---

## Acceptance status against the specification

| # | Criterion | Status |
| --- | --- | --- |
| 1 | OAuth state/PKCE/redirect validation, refresh, revoked tokens | Implemented; refresh/revoked/exchange unit-tested; DB-backed state flow needs integration test |
| 2 | Secrets absent from DB, logs, browser, AI prompts | Implemented (vault + redaction); unit-tested for redaction/vault; no live DB dump audit |
| 3 | SMTP TLS with certificate validation | Implemented and unit-tested; live handshake not run |
| 4 | Custom SMTP SSRF blocking | Implemented and unit-tested for address classes |
| 5 | Idempotency prevents duplicates | Implemented (unique index + fingerprint check); unit-tested helpers; DB concurrency not run |
| 6 | Expired worker lease recoverable | Implemented; needs DB integration test |
| 7 | Quota reservations prevent overrun | Implemented (row-locked reserve/consume/release); needs DB concurrency test |
| 8 | Acceptance never sets delivered | Implemented and provider-tested; finalize path needs DB test |
| 9 | Backoff/retry policy | Implemented and unit-tested |
| 10 | Global pause blocks new submissions | Implemented; UI explains recall limits; needs DB/E2E test |
| 11 | Diagnostic sends require allowlisted verified recipients | Implemented; needs DB/E2E test |
| 12 | Hard bounces/complaints suppressed globally | Implemented; pure blocking rules unit-tested; DB behavior needs test |
| 13 | Circuit breaker opens/recovers | Implemented; decision rules unit-tested; DB transitions need test |
| 14 | Readiness gate blocks production | Implemented and unit-tested |
| 15 | Encrypted backup/restore | Encryption format unit-tested; `pg_dump`/`pg_restore` not executed here |

---

## Troubleshooting

**`/readyz` returns 503 `secret_store: unavailable`** — check `VAULT_DIR`
permissions and `VAULT_MASTER_KEY`. In Compose the volume is `/data`; the
container runs as UID 1001.

**Login says invalid credentials** — `ADMIN_PASSWORD_HASH` wins over
`ADMIN_PASSWORD`. Re-run `python -m app.cli hash-password` and restart the API.
Production rejects `ADMIN_PASSWORD` shorter than 12 characters.

**OAuth redirect mismatch** — the registered URI must exactly match
`${PUBLIC_BASE_URL}/api/v1/accounts/oauth/<provider>/callback`, including
scheme, port, and trailing path.

**Account stays `test_only`** — open the readiness panel. Required SPF/DKIM,
connection, credentials, sender identity, or acknowledgements are missing.
Blocking checks cannot be bypassed; warnings require explicit acknowledgement.

**Circuit is open and will not retry** — credential, quota, TLS, and complaint
circuits require a manual reset with a reason in the account's Policy tab.
Transient circuits half-open automatically after their window.

**Message is `needs_review`** — the provider outcome was ambiguous. Inspect the
attempt and events, then either requeue with the explicit duplicate-risk
confirmation or leave it. Automatic retry is intentionally disabled.

**`account_test_only` when calling the API** — transactional sends require
`mode=production`; diagnostic tests use the Tests endpoints.

**Web UI shows API errors / 404 on `/api/v1`** — the rewrite target is baked at
build time. Rebuild the web image with `API_INTERNAL_URL` reachable from inside
the web container (`http://api:8000` in Compose).

**Database volume reset** — `docker compose down -v` deletes `pgdata` **and**
`appdata` (the vault). Back up first.

---

## License and scope

Private use by the repository owner. No warranty. This tool is not an
anti-spam system, not an inbox-warming service, and not a bulk mailer. Use it
only with mailboxes and recipients you are authorized to contact, and comply
with the policies of every connected provider and applicable law.
