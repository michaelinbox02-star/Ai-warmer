"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { formatDate, formatRelative, humanize } from "@/lib/format";
import { useApiData, useMutation } from "@/lib/use-api-data";
import type {
  Account,
  AccountMode,
  DomainCheck,
  MessageSummary,
  ReadinessCheck,
  TestRun,
} from "@/lib/types";
import {
  Badge,
  Card,
  Code,
  EmptyState,
  Field,
  KeyValue,
  Loading,
  Meter,
  Notice,
  PageHeader,
  StatusBadge,
} from "@/components/ui";

type Tab = "overview" | "checks" | "tests" | "messages" | "policy" | "credentials";

const TABS: Array<{ id: Tab; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "checks", label: "Domain checks" },
  { id: "tests", label: "Test results" },
  { id: "messages", label: "Message log" },
  { id: "policy", label: "Policy" },
  { id: "credentials", label: "Credentials" },
];

const NON_ACKNOWLEDGEABLE = new Set(["account_mode", "global_sending"]);

export default function AccountDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const id = typeof params?.id === "string" ? params.id : null;

  const account = useApiData<Account>(id ? `/accounts/${id}` : null);
  const checks = useApiData<{ latest: Record<string, string>; history: DomainCheck[] }>(
    id ? `/accounts/${id}/domain-checks` : null,
  );
  const runs = useApiData<{ test_runs: TestRun[] }>(id ? `/tests?account_id=${id}&limit=10` : null);
  const messages = useApiData<{ messages: MessageSummary[] }>(
    id ? `/messages?account_id=${id}&limit=10` : null,
  );

  const mutation = useMutation();
  const [tab, setTab] = useState<Tab>("overview");
  const [notice, setNotice] = useState<{ tone: "success" | "danger" | "info"; text: string } | null>(
    null,
  );
  const [reason, setReason] = useState("");
  const [modeTarget, setModeTarget] = useState<AccountMode>("test_only");
  const [acknowledged, setAcknowledged] = useState<string[]>([]);
  const [circuitReason, setCircuitReason] = useState("Manual reset after reviewing the failure");
  const [testResult, setTestResult] = useState<Record<string, unknown> | null>(null);

  const [identity, setIdentity] = useState({ sending_domain: "", verified: true, note: "" });
  const [policy, setPolicy] = useState({
    max_per_minute: "30",
    max_rolling_24h: "200",
    max_attempts: "5",
    max_in_flight: "1",
    require_auth_checks: true,
    require_test_recipient_allowlist: true,
    reason: "",
  });
  const [smtp, setSmtp] = useState({ username: "", password: "" });

  useEffect(() => {
    const search = new URLSearchParams(window.location.search);
    if (search.get("connected") === "1") {
      setNotice({ tone: "success", text: "OAuth connected. Run a connection test to confirm." });
    }
    const oauthError = search.get("oauth_error");
    if (oauthError) {
      setNotice({ tone: "danger", text: `OAuth failed: ${oauthError}` });
    }
  }, []);

  useEffect(() => {
    const data = account.data;
    if (!data) return;
    setIdentity((current) => ({
      ...current,
      sending_domain: data.sending_domain || "",
      verified: data.provider_metadata?.sender_identity_verified === true,
    }));
    if (data.policy) {
      setPolicy((current) => ({
        ...current,
        max_per_minute: String(data.policy?.max_per_minute ?? 30),
        max_rolling_24h: String(data.policy?.max_rolling_24h ?? 200),
        max_attempts: String(data.policy?.max_attempts ?? 5),
        max_in_flight: String(data.policy?.max_in_flight ?? 1),
        require_auth_checks: data.policy?.require_auth_checks ?? true,
        require_test_recipient_allowlist:
          data.policy?.require_test_recipient_allowlist ?? true,
      }));
      setModeTarget(data.policy.mode);
    }
    setSmtp((current) => ({
      username: String(data.provider_metadata?.smtp_username ?? ""),
      password: current.password,
    }));
  }, [account.data]);

  function reloadAll() {
    account.reload();
    checks.reload();
    runs.reload();
    messages.reload();
  }

  async function runAction(
    action: () => Promise<unknown>,
    success: string,
    after?: () => void,
  ) {
    setNotice(null);
    const result = await mutation.run(action, success);
    if (result !== null) {
      if (after) after();
      reloadAll();
    }
  }

  async function testConnection() {
    if (!id) return;
    setTestResult(null);
    const result = await mutation.run(
      () => api<Record<string, unknown>>(`/accounts/${id}/test-connection`, { method: "POST" }),
      "",
    );
    if (result) {
      setTestResult(result);
      setNotice({
        tone: result.ok ? "success" : "danger",
        text: `${result.ok ? "Connection validated" : "Connection failed"} — ${String(result.stage)} / ${String(result.code)}`,
      });
      reloadAll();
    }
  }

  async function runChecks() {
    if (!id) return;
    await runAction(
      () =>
        api(`/accounts/${id}/domain-checks`, {
          method: "POST",
          body: { check_types: null },
        }),
      "Domain checks completed. Review the evidence below.",
    );
  }

  async function changeMode(mode: AccountMode) {
    if (!id) return;
    const acknowledgements =
      mode === "production"
        ? (account.data?.readiness?.checks || [])
            .filter(
              (check) =>
                check.status === "warning" && check.blocking && !NON_ACKNOWLEDGEABLE.has(check.key),
            )
            .map((check) => check.key)
        : [];
    await runAction(
      () =>
        api(`/accounts/${id}/mode`, {
          method: "POST",
          body: {
            mode,
            acknowledgements: mode === "production" ? acknowledgements : [],
            reason: reason.trim() || null,
          },
        }),
      `Account mode set to ${humanize(mode)}.`,
    );
    setReason("");
  }

  async function savePolicy() {
    if (!id) return;
    await runAction(
      () =>
        api(`/accounts/${id}/policy`, {
          method: "POST",
          body: {
            max_per_minute: Number(policy.max_per_minute),
            max_rolling_24h: Number(policy.max_rolling_24h),
            max_attempts: Number(policy.max_attempts),
            max_in_flight: Number(policy.max_in_flight),
            require_auth_checks: policy.require_auth_checks,
            require_test_recipient_allowlist: policy.require_test_recipient_allowlist,
            reason: policy.reason.trim() || null,
          },
        }),
      "Policy saved. New limits apply to the next submission.",
    );
  }

  async function resetCircuit() {
    if (!id) return;
    await runAction(
      () =>
        api(`/accounts/${id}/circuit/reset`, {
          method: "POST",
          body: { reason: circuitReason.trim() },
        }),
      "Circuit reset. The next submission will be watched closely.",
    );
  }

  async function acknowledgeWarnings() {
    if (!id) return;
    await runAction(
      () =>
        api(`/accounts/${id}/acknowledge-warnings`, {
          method: "POST",
          body: { keys: acknowledged, note: reason.trim() || null },
        }),
      "Warnings acknowledged.",
    );
    setAcknowledged([]);
  }

  async function saveIdentity() {
    if (!id) return;
    await runAction(
      () =>
        api(`/accounts/${id}/sender-identity`, {
          method: "POST",
          body: {
            sending_domain: identity.sending_domain.trim(),
            verified: identity.verified,
            note: identity.note.trim() || null,
          },
        }),
      "Sender identity updated.",
    );
  }

  async function rotateSmtp() {
    if (!id) return;
    await runAction(
      () =>
        api(`/accounts/${id}/credentials/smtp`, {
          method: "POST",
          body: {
            password: smtp.password,
            username: smtp.username.trim() || null,
          },
        }),
      "SMTP credential replaced. The account is pending until a connection test succeeds.",
    );
    setSmtp((current) => ({ ...current, password: "" }));
  }

  async function reauthorize(provider: string) {
    const result = await mutation.run(
      () => api<{ authorize_url: string }>(`/accounts/oauth/${provider}/start`, { method: "POST" }),
      "",
    );
    if (result?.authorize_url) window.location.assign(result.authorize_url);
  }

  async function disableAccount() {
    if (!id) return;
    const confirmed = window.confirm(
      "Disable this account? No further submissions will be attempted. This cannot be undone from the UI.",
    );
    if (!confirmed) return;
    await runAction(
      () =>
        api(`/accounts/${id}/disable`, {
          method: "POST",
          body: { reason: reason.trim() || "Disabled by operator from the account console" },
        }),
      "Account disabled and sending paused.",
    );
  }

  async function deleteAccount() {
    if (!id) return;
    const confirmed = window.confirm(
      "Delete this account permanently? Accounts with message history cannot be deleted.",
    );
    if (!confirmed) return;
    setNotice(null);
    const result = await mutation.run(
      () => api(`/accounts/${id}`, { method: "DELETE" }),
      "",
    );
    if (result !== null) router.replace("/accounts");
  }

  if (!id) return <Notice tone="danger">Missing account identifier.</Notice>;
  if (account.loading && !account.data) return <Loading label="Loading account…" />;
  if (account.error) {
    return (
      <>
        <PageHeader title="Account" />
        <Notice tone="danger" title="Could not load this account">
          {account.error}
        </Notice>
        <Link className="button secondary" href="/accounts">
          Back to accounts
        </Link>
      </>
    );
  }
  if (!account.data) return null;

  const data = account.data;
  const readiness = data.readiness;
  const acknowledgeable = (readiness?.checks || []).filter(
    (check) =>
      check.status === "warning" && check.blocking && !NON_ACKNOWLEDGEABLE.has(check.key),
  );
  const hardBlockers = (readiness?.checks || []).filter(
    (check) => check.blocking && check.status !== "warning" && !NON_ACKNOWLEDGEABLE.has(check.key),
  );
  const smtpMeta = data.provider_metadata || {};
  const oauthProvider = ["gmail_api", "microsoft_graph"].includes(data.transport)
    ? data.provider
    : null;

  return (
    <>
      <div className="detail-header">
        <Link className="back-link" href="/accounts">
          ← All accounts
        </Link>
        <div className="detail-title">
          <h1>{data.mailbox_email}</h1>
          <StatusBadge status={data.state} />
          {data.policy && <StatusBadge status={data.policy.mode} />}
          <Badge tone={readiness?.ready ? "success" : "warning"}>
            {readiness?.ready ? "production ready" : "not ready"}
          </Badge>
        </div>
        <div className="chip-row">
          <Badge>{data.provider.replaceAll("_", " ")}</Badge>
          <Badge>{data.transport.replaceAll("_", " ")}</Badge>
          <span className="muted small">domain {data.sending_domain || "not set"}</span>
          <span className="muted small">tested {formatRelative(data.connection_tested_at)}</span>
        </div>
      </div>

      {notice && <Notice tone={notice.tone}>{notice.text}</Notice>}
      {mutation.error && (
        <Notice tone="danger" title="Action failed">
          {mutation.error}
          {mutation.issues.length > 0 && (
            <ul>
              {mutation.issues.map((issue) => (
                <li key={issue}>{issue}</li>
              ))}
            </ul>
          )}
        </Notice>
      )}
      {data.state === "disabled" && (
        <Notice tone="warning" title="Account disabled">
          No further submissions will be attempted for this mailbox. Historical evidence remains.
        </Notice>
      )}
      {data.last_error_code && (
        <Notice tone="danger" title={`Last error: ${data.last_error_code}`}>
          {data.last_error_summary}
        </Notice>
      )}
      {data.policy?.circuit.state === "open" && (
        <Notice tone="danger" title="Sending circuit is open">
          {data.policy.circuit.reason}
          {data.policy.circuit.open_until && (
            <div>Automatic close not before {formatDate(data.policy.circuit.open_until)}.</div>
          )}
          {data.policy.circuit.requires_manual_reset && (
            <div>A manual reset is required after reviewing the cause.</div>
          )}
        </Notice>
      )}

      <div className="tabs" role="tablist" aria-label="Account sections">
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={tab === item.id}
            className={tab === item.id ? "active" : ""}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {tab === "overview" && (
        <div className="tab-panel">
          <div className="grid two-columns">
            <Card
              title="Connection"
              subtitle="A connection test validates the transport. It does not send mail."
              actions={
                <button
                  className="button primary small"
                  type="button"
                  onClick={() => void testConnection()}
                  disabled={mutation.busy}
                >
                  {mutation.busy ? "Testing…" : "Test connection"}
                </button>
              }
            >
              <KeyValue
                items={[
                  { label: "State", value: <StatusBadge status={data.state} /> },
                  { label: "Last tested", value: formatDate(data.connection_tested_at) },
                  { label: "Mailbox", value: data.mailbox_email },
                  { label: "Display name", value: data.display_name || "—" },
                  { label: "Provider", value: data.provider.replaceAll("_", " ") },
                  { label: "Transport", value: data.transport.replaceAll("_", " ") },
                ]}
              />
              {testResult && (
                <Code block>{JSON.stringify(testResult, null, 2)}</Code>
              )}
            </Card>

            <Card
              title="Readiness gate"
              subtitle="Production is blocked until every required check passes or is acknowledged."
            >
              {readiness ? (
                <>
                  <div className="chip-row">
                    <Badge tone={readiness.ready ? "success" : "warning"}>
                      {readiness.ready ? "ready" : "blocked"}
                    </Badge>
                    {readiness.blocking.length > 0 && (
                      <span className="muted small">
                        blocking: {readiness.blocking.map(humanize).join(", ")}
                      </span>
                    )}
                  </div>
                  <ul className="check-list">
                    {readiness.checks.map((check) => (
                      <ReadinessRow key={check.key} check={check} />
                    ))}
                  </ul>
                </>
              ) : (
                <EmptyState title="Readiness unavailable" />
              )}
            </Card>
          </div>

          <div className="grid two-columns">
            <Card
              title="Sending mode"
              subtitle="Production permits transactional mail. Test-only permits controlled tests only."
            >
              <div className="form-grid">
                <Field label="Target mode" htmlFor="mode" hint="Production requires the readiness gate.">
                  <select
                    id="mode"
                    value={modeTarget}
                    onChange={(event) => setModeTarget(event.target.value as AccountMode)}
                  >
                    <option value="test_only">Test only</option>
                    <option value="paused">Paused</option>
                    <option value="production">Production</option>
                  </select>
                </Field>
                <Field label="Reason" htmlFor="mode_reason" hint="Recorded in the audit log.">
                  <input
                    id="mode_reason"
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                    placeholder="Why is the mode changing?"
                  />
                </Field>
              </div>

              {modeTarget === "production" && acknowledgeable.length > 0 && (
                <Notice tone="warning" title="Acknowledge these warnings to continue">
                  <ul className="ack-list">
                    {acknowledgeable.map((check) => (
                      <li key={check.key}>
                        <input
                          id={`ack_${check.key}`}
                          type="checkbox"
                          checked={acknowledged.includes(check.key)}
                          onChange={(event) =>
                            setAcknowledged((current) =>
                              event.target.checked
                                ? [...current, check.key]
                                : current.filter((key) => key !== check.key),
                            )
                          }
                        />
                        <label htmlFor={`ack_${check.key}`}>
                          <strong>{check.label}</strong> — {check.detail}
                        </label>
                      </li>
                    ))}
                  </ul>
                </Notice>
              )}

              {modeTarget === "production" && hardBlockers.length > 0 && (
                <Notice tone="danger" title="These checks must pass first">
                  <ul>
                    {hardBlockers.map((check) => (
                      <li key={check.key}>
                        {check.label}: {check.detail}
                      </li>
                    ))}
                  </ul>
                </Notice>
              )}

              <div className="button-row">
                <button
                  className="button primary"
                  type="button"
                  onClick={() => void changeMode(modeTarget)}
                  disabled={
                    mutation.busy ||
                    (modeTarget === "production" &&
                      (acknowledgeable.some((check) => !acknowledged.includes(check.key)) ||
                        hardBlockers.length > 0))
                  }
                >
                  {mutation.busy ? "Applying…" : `Set ${humanize(modeTarget)}`}
                </button>
                <button
                  className="button secondary"
                  type="button"
                  onClick={() => void changeMode("paused")}
                  disabled={mutation.busy || data.policy?.mode === "paused"}
                >
                  Pause account
                </button>
              </div>
            </Card>

            <Card title="Quota and circuit" subtitle="Reservations are taken before every submission attempt.">
              {data.quota && (
                <div className="stack">
                  <Meter
                    label="Per minute"
                    used={data.quota.per_minute_used}
                    limit={data.quota.per_minute_limit}
                  />
                  <Meter
                    label="Rolling 24 hours"
                    used={data.quota.rolling_24h_used}
                    limit={data.quota.rolling_24h_limit}
                  />
                  <Meter
                    label="In flight"
                    used={data.quota.in_flight}
                    limit={data.quota.max_in_flight}
                  />
                </div>
              )}
              <KeyValue
                items={[
                  {
                    label: "Circuit",
                    value: <StatusBadge status={data.policy?.circuit.state || "unknown"} />,
                  },
                  { label: "Reason", value: data.policy?.circuit.reason || "—" },
                  {
                    label: "Open until",
                    value: data.policy?.circuit.open_until
                      ? formatDate(data.policy.circuit.open_until)
                      : "—",
                  },
                  { label: "Policy updated", value: formatDate(data.policy?.updated_at) },
                ]}
              />
              <div className="switch-actions">
                <input
                  aria-label="Reason for resetting the circuit"
                  value={circuitReason}
                  onChange={(event) => setCircuitReason(event.target.value)}
                  minLength={3}
                  maxLength={500}
                />
                <button
                  className="button secondary"
                  type="button"
                  onClick={() => void resetCircuit()}
                  disabled={mutation.busy || circuitReason.trim().length < 3}
                >
                  Reset circuit
                </button>
              </div>
            </Card>
          </div>

          <Card
            title="Sender identity"
            subtitle="Sender addresses must match the mailbox or an explicitly allowed alias."
          >
            <div className="form-grid">
              <Field label="Sending domain" htmlFor="sending_domain" required>
                <input
                  id="sending_domain"
                  value={identity.sending_domain}
                  onChange={(event) =>
                    setIdentity((current) => ({ ...current, sending_domain: event.target.value }))
                  }
                />
              </Field>
              <Field label="Verification note" htmlFor="identity_note">
                <input
                  id="identity_note"
                  value={identity.note}
                  onChange={(event) =>
                    setIdentity((current) => ({ ...current, note: event.target.value }))
                  }
                  placeholder="How was domain ownership confirmed?"
                />
              </Field>
              <div className="field checkbox">
                <input
                  id="identity_verified"
                  type="checkbox"
                  checked={identity.verified}
                  onChange={(event) =>
                    setIdentity((current) => ({ ...current, verified: event.target.checked }))
                  }
                />
                <label htmlFor="identity_verified">
                  I have verified that this mailbox may send as the sending domain.
                </label>
              </div>
              <div className="form-actions">
                <button
                  className="button primary"
                  type="button"
                  onClick={() => void saveIdentity()}
                  disabled={mutation.busy || identity.sending_domain.trim().length < 3}
                >
                  Save sender identity
                </button>
              </div>
            </div>
          </Card>

          <Card
            title="Danger zone"
            subtitle="Disabling stops future submissions. Deletion is only possible without message history."
          >
            <div className="switch-actions">
              <input
                aria-label="Reason for disabling the account"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder="Reason (recorded in the audit log)"
              />
              <button
                className="button secondary"
                type="button"
                onClick={() => void disableAccount()}
                disabled={mutation.busy || data.state === "disabled" || reason.trim().length < 3}
              >
                Disable account
              </button>
              <button
                className="button danger"
                type="button"
                onClick={() => void deleteAccount()}
                disabled={mutation.busy}
              >
                Delete account
              </button>
            </div>
          </Card>
        </div>
      )}

      {tab === "checks" && (
        <div className="tab-panel">
          <Card
            title="DNS and authentication checks"
            subtitle="SPF, DKIM, DMARC, MX, TLS, and sender alignment with observed evidence."
            actions={
              <button
                className="button primary small"
                type="button"
                onClick={() => void runChecks()}
                disabled={mutation.busy}
              >
                {mutation.busy ? "Checking…" : "Run all checks"}
              </button>
            }
          >
            {checks.loading && !checks.data ? (
              <Loading />
            ) : checks.error ? (
              <Notice tone="danger">{checks.error}</Notice>
            ) : (
              <>
                <KeyValue
                  columns={3}
                  items={Object.entries(checks.data?.latest || {}).map(([key, value]) => ({
                    label: key.toUpperCase(),
                    value: <StatusBadge status={value} />,
                  }))}
                />
                {(checks.data?.history || []).length === 0 ? (
                  <EmptyState title="No checks recorded yet">
                    Run the checks to collect evidence for the readiness gate.
                  </EmptyState>
                ) : (
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Type</th>
                          <th>Status</th>
                          <th>Selector</th>
                          <th>Observed</th>
                          <th>Checked</th>
                          <th>Evidence</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(checks.data?.history || []).map((row) => (
                          <tr key={row.id}>
                            <td>{row.check_type.toUpperCase()}</td>
                            <td>
                              <StatusBadge status={row.status} />
                            </td>
                            <td>{row.selector || "—"}</td>
                            <td className="wrap">{row.observed_value || "—"}</td>
                            <td>{formatDate(row.checked_at)}</td>
                            <td className="wrap">
                              <details>
                                <summary>View</summary>
                                <Code block>{JSON.stringify(row.evidence, null, 2)}</Code>
                              </details>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </>
            )}
          </Card>
        </div>
      )}

      {tab === "tests" && (
        <Card
          title="Controlled test runs"
          subtitle="Each destination must be on the verified allowlist. Acceptance is not placement."
          actions={
            <Link className="button secondary small" href="/tests">
              Open test console
            </Link>
          }
        >
          {runs.loading && !runs.data ? (
            <Loading />
          ) : (runs.data?.test_runs || []).length === 0 ? (
            <EmptyState title="No test runs for this account">
              Create a run from the controlled test console after adding a verified recipient.
            </EmptyState>
          ) : (
            <div className="account-list">
              {(runs.data?.test_runs || []).map((run) => (
                <Link className="account-row" href={`/tests/${run.id}`} key={run.id}>
                  <div className="account-main">
                    <div className="account-title">
                      <strong>{humanize(run.test_profile)}</strong>
                      <StatusBadge status={run.status} />
                    </div>
                    <div className="account-meta">
                      <span>{run.results.length} recipient(s)</span>
                      <span>created {formatRelative(run.created_at)}</span>
                      <span>by {run.initiated_by}</span>
                    </div>
                  </div>
                  <div className="account-side">
                    <Badge tone="info">
                      {run.results.filter((result) => result.status === "observed_received").length}{" "}
                      observed
                    </Badge>
                  </div>
                </Link>
              ))}
            </div>
          )}
        </Card>
      )}

      {tab === "messages" && (
        <Card
          title="Message log"
          subtitle="Provider acceptance is recorded as submitted. Delivery evidence is separate."
          actions={
            <Link className="button secondary small" href={`/messages?account_id=${data.id}`}>
              Open message log
            </Link>
          }
        >
          {messages.loading && !messages.data ? (
            <Loading />
          ) : (messages.data?.messages || []).length === 0 ? (
            <EmptyState title="No messages for this account" />
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Recipient</th>
                    <th>Subject</th>
                    <th>Status</th>
                    <th>Attempts</th>
                    <th>Created</th>
                  </tr>
                </thead>
                <tbody>
                  {(messages.data?.messages || []).map((message) => (
                    <tr key={message.id}>
                      <td>
                        <Link href={`/messages/${message.id}`}>{message.to}</Link>
                      </td>
                      <td className="wrap">{message.subject}</td>
                      <td>
                        <StatusBadge status={message.status} />
                      </td>
                      <td>{message.attempt_count}</td>
                      <td>{formatDate(message.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {tab === "policy" && (
        <div className="tab-panel">
          <Card
            title="Sending limits"
            subtitle="Conservative defaults are enforced by the worker before any provider call."
          >
            <div className="form-grid">
              <Field label="Max per minute" htmlFor="max_per_minute" required>
                <input
                  id="max_per_minute"
                  type="number"
                  min={1}
                  max={600}
                  value={policy.max_per_minute}
                  onChange={(event) =>
                    setPolicy((current) => ({ ...current, max_per_minute: event.target.value }))
                  }
                />
              </Field>
              <Field label="Max rolling 24h" htmlFor="max_rolling_24h" required>
                <input
                  id="max_rolling_24h"
                  type="number"
                  min={1}
                  max={100000}
                  value={policy.max_rolling_24h}
                  onChange={(event) =>
                    setPolicy((current) => ({ ...current, max_rolling_24h: event.target.value }))
                  }
                />
              </Field>
              <Field label="Max attempts" htmlFor="max_attempts" required>
                <input
                  id="max_attempts"
                  type="number"
                  min={1}
                  max={10}
                  value={policy.max_attempts}
                  onChange={(event) =>
                    setPolicy((current) => ({ ...current, max_attempts: event.target.value }))
                  }
                />
              </Field>
              <Field label="Max in flight" htmlFor="max_in_flight" required>
                <input
                  id="max_in_flight"
                  type="number"
                  min={1}
                  max={5}
                  value={policy.max_in_flight}
                  onChange={(event) =>
                    setPolicy((current) => ({ ...current, max_in_flight: event.target.value }))
                  }
                />
              </Field>
              <Field label="Change reason" htmlFor="policy_reason">
                <input
                  id="policy_reason"
                  value={policy.reason}
                  onChange={(event) =>
                    setPolicy((current) => ({ ...current, reason: event.target.value }))
                  }
                  placeholder="Optional, recorded in the audit log"
                />
              </Field>
              <div className="field checkbox">
                <input
                  id="require_auth_checks"
                  type="checkbox"
                  checked={policy.require_auth_checks}
                  onChange={(event) =>
                    setPolicy((current) => ({
                      ...current,
                      require_auth_checks: event.target.checked,
                    }))
                  }
                />
                <label htmlFor="require_auth_checks">
                  Require passing authentication checks for production.
                </label>
              </div>
              <div className="field checkbox">
                <input
                  id="require_allowlist"
                  type="checkbox"
                  checked={policy.require_test_recipient_allowlist}
                  onChange={(event) =>
                    setPolicy((current) => ({
                      ...current,
                      require_test_recipient_allowlist: event.target.checked,
                    }))
                  }
                />
                <label htmlFor="require_allowlist">
                  Require verified allowlist recipients for diagnostic tests.
                </label>
              </div>
              <div className="form-actions">
                <button
                  className="button primary"
                  type="button"
                  onClick={() => void savePolicy()}
                  disabled={mutation.busy}
                >
                  Save policy
                </button>
              </div>
            </div>
          </Card>

          <Card
            title="Acknowledge warnings"
            subtitle="Acknowledging a warning never turns a failure into a pass."
          >
            {acknowledgeable.length === 0 ? (
              <EmptyState title="No acknowledgeable warnings" />
            ) : (
              <>
                <ul className="ack-list">
                  {acknowledgeable.map((check) => (
                    <li key={check.key}>
                      <input
                        id={`policy_ack_${check.key}`}
                        type="checkbox"
                        checked={acknowledged.includes(check.key)}
                        onChange={(event) =>
                          setAcknowledged((current) =>
                            event.target.checked
                              ? [...current, check.key]
                              : current.filter((key) => key !== check.key),
                          )
                        }
                      />
                      <label htmlFor={`policy_ack_${check.key}`}>
                        <strong>{check.label}</strong> — {check.detail}
                      </label>
                    </li>
                  ))}
                </ul>
                <div className="button-row">
                  <input
                    aria-label="Acknowledgement note"
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                    placeholder="Note for the audit log"
                  />
                  <button
                    className="button secondary"
                    type="button"
                    onClick={() => void acknowledgeWarnings()}
                    disabled={mutation.busy || acknowledged.length === 0}
                  >
                    Acknowledge selected
                  </button>
                </div>
              </>
            )}
          </Card>
        </div>
      )}

      {tab === "credentials" && (
        <div className="tab-panel">
          <Card
            title="Stored credential metadata"
            subtitle="Secret values live in the secret store and are never returned to the browser."
          >
            {data.credentials ? (
              <KeyValue
                items={[
                  { label: "Secret backend", value: data.credentials.backend },
                  { label: "Credential version", value: data.credentials.credential_version },
                  { label: "Configured", value: data.credentials.configured ? "yes" : "no" },
                  {
                    label: "Access token expires",
                    value: formatDate(data.credentials.access_token_expires_at),
                  },
                  { label: "Last refresh", value: formatDate(data.credentials.last_refresh_at) },
                  { label: "Last validated", value: formatDate(data.credentials.last_validated_at) },
                  {
                    label: "Granted scopes",
                    value:
                      data.credentials.granted_scopes.length > 0 ? (
                        <div className="chip-row">
                          {data.credentials.granted_scopes.map((scope) => (
                            <Badge key={scope}>{scope.split("/").pop()}</Badge>
                          ))}
                        </div>
                      ) : (
                        "—"
                      ),
                  },
                ]}
              />
            ) : (
              <EmptyState title="No credential stored">
                Connect the account through OAuth or save an SMTP password.
              </EmptyState>
            )}
            {oauthProvider && (
              <div className="button-row">
                <button
                  className="button secondary"
                  type="button"
                  onClick={() => void reauthorize(oauthProvider)}
                  disabled={mutation.busy}
                >
                  Reauthorize with {oauthProvider.replaceAll("_", " ")}
                </button>
                <span className="muted small">
                  Uses PKCE and single-use state. The old refresh token is replaced.
                </span>
              </div>
            )}
          </Card>

          {data.transport === "smtp" ? (
            <Card
              title="Rotate SMTP password"
              subtitle="Saving a new password sets the account to pending until the next connection test."
            >
              <div className="form-grid">
                <Field label="SMTP username" htmlFor="smtp_username">
                  <input
                    id="smtp_username"
                    value={smtp.username}
                    onChange={(event) =>
                      setSmtp((current) => ({ ...current, username: event.target.value }))
                    }
                    autoComplete="username"
                  />
                </Field>
                <Field label="New password" htmlFor="smtp_password" required>
                  <input
                    id="smtp_password"
                    type="password"
                    value={smtp.password}
                    onChange={(event) =>
                      setSmtp((current) => ({ ...current, password: event.target.value }))
                    }
                    autoComplete="new-password"
                  />
                </Field>
                <div className="form-actions">
                  <button
                    className="button primary"
                    type="button"
                    onClick={() => void rotateSmtp()}
                    disabled={mutation.busy || smtp.password.length === 0}
                  >
                    Save new password
                  </button>
                </div>
              </div>
              <KeyValue
                items={[
                  { label: "Host", value: String(smtpMeta.smtp_host || "—") },
                  { label: "Port", value: String(smtpMeta.smtp_port || "—") },
                  { label: "Security", value: String(smtpMeta.smtp_security || "—") },
                  {
                    label: "DKIM selectors",
                    value: Array.isArray(smtpMeta.dkim_selectors)
                      ? smtpMeta.dkim_selectors.join(", ") || "—"
                      : "—",
                  },
                ]}
              />
            </Card>
          ) : (
            <Notice tone="info">
              This account uses a provider API transport. Credentials are managed through the OAuth
              consent flow and rotated by the backend; there is no password to display or edit.
            </Notice>
          )}
        </div>
      )}
    </>
  );
}

function ReadinessRow({ check }: { check: ReadinessCheck }) {
  return (
    <li>
      <StatusBadge status={check.status} />
      <div className="check-body">
        <strong>
          {check.label}
          {check.acknowledged && <span className="muted small"> · acknowledged</span>}
        </strong>
        <span>{check.detail}</span>
      </div>
    </li>
  );
}
