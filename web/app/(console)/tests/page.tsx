"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { api, query } from "@/lib/api";
import { formatDate, formatRelative, humanize } from "@/lib/format";
import { useApiData, useMutation } from "@/lib/use-api-data";
import type { Account, TestRecipient, TestRun } from "@/lib/types";
import {
  Badge,
  Card,
  EmptyState,
  Field,
  Loading,
  Notice,
  PageHeader,
  StatusBadge,
} from "@/components/ui";

type Tab = "runs" | "allowlist";

const PROFILES = [
  { id: "basic_delivery", label: "Basic delivery", hint: "Send and wait for observation" },
  { id: "authentication", label: "Authentication", hint: "Focus on SPF/DKIM/DMARC headers" },
  { id: "latency", label: "Latency", hint: "Measure submission and observation time" },
];

export default function ControlledTestsPage() {
  const [tab, setTab] = useState<Tab>("runs");
  const [accountId, setAccountId] = useState("");
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);

  const accounts = useApiData<{ accounts: Account[] }>("/accounts");
  const recipients = useApiData<{ recipients: TestRecipient[] }>(
    "/tests/recipients?include_disabled=true",
  );
  const runs = useApiData<{
    test_runs: TestRun[];
    has_more: boolean;
    next_offset: number | null;
  }>(`/tests${query({ account_id: accountId, status, limit: 50, offset })}`);

  const mutation = useMutation();
  const [notice, setNotice] = useState<{ tone: "success" | "danger"; text: string } | null>(null);

  const [recipientForm, setRecipientForm] = useState({
    recipient_email: "",
    label: "",
    verification_method: "operator_confirmed",
    attest: false,
  });

  const [runForm, setRunForm] = useState({
    account_id: "",
    profile: "basic_delivery",
    recipient_ids: [] as string[],
    recipient_emails: "",
  });

  useEffect(() => {
    if (!runForm.account_id && accounts.data?.accounts.length === 1) {
      setRunForm((current) => ({ ...current, account_id: accounts.data!.accounts[0].id }));
    }
  }, [accounts.data, runForm.account_id]);

  const enabledRecipients = (recipients.data?.recipients || []).filter(
    (recipient) => recipient.is_enabled,
  );

  function toggleRecipient(id: string, checked: boolean) {
    setRunForm((current) => ({
      ...current,
      recipient_ids: checked
        ? [...current.recipient_ids, id]
        : current.recipient_ids.filter((value) => value !== id),
    }));
  }

  async function addRecipient(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setNotice(null);
    const result = await mutation.run(
      () =>
        api("/tests/recipients", {
          method: "POST",
          body: {
            recipient_email: recipientForm.recipient_email.trim(),
            label: recipientForm.label.trim() || null,
            verification_method: recipientForm.verification_method,
            attest_authorized: recipientForm.attest,
          },
        }),
      "",
    );
    if (result) {
      setNotice({
        tone: "success",
        text: "Recipient added. Only enabled, verified allowlist entries can receive tests.",
      });
      setRecipientForm({
        recipient_email: "",
        label: "",
        verification_method: "operator_confirmed",
        attest: false,
      });
      recipients.reload();
    }
  }

  async function toggleRecipientState(recipient: TestRecipient, enabled: boolean) {
    setNotice(null);
    const result = await mutation.run(
      () =>
        api(`/tests/recipients/${recipient.id}`, {
          method: "PATCH",
          body: { enabled, reason: enabled ? "Re-enabled by operator" : "Disabled by operator" },
        }),
      "",
    );
    if (result) {
      setNotice({ tone: "success", text: `Recipient ${enabled ? "enabled" : "disabled"}.` });
      recipients.reload();
    }
  }

  async function deleteRecipient(recipient: TestRecipient) {
    if (!window.confirm(`Remove ${recipient.recipient_email} from the allowlist?`)) return;
    setNotice(null);
    const result = await mutation.run(
      () => api(`/tests/recipients/${recipient.id}`, { method: "DELETE" }),
      "",
    );
    if (result !== null) {
      setNotice({ tone: "success", text: "Recipient removed. Historical evidence is preserved." });
      recipients.reload();
    }
  }

  async function createRun(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setNotice(null);
    const emails = runForm.recipient_emails
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);
    const result = await mutation.run(
      () =>
        api<{ test_run: TestRun }>("/tests", {
          method: "POST",
          body: {
            account_id: runForm.account_id,
            profile: runForm.profile,
            recipient_ids: runForm.recipient_ids,
            recipient_emails: emails,
          },
        }),
      "",
    );
    if (result) {
      setNotice({
        tone: "success",
        text: "Test run queued. Acceptance is not placement — record the received headers.",
      });
      setRunForm((current) => ({ ...current, recipient_ids: [], recipient_emails: "" }));
      runs.reload();
    }
  }

  return (
    <>
      <PageHeader
        title="Controlled tests"
        description="Diagnostic mail only to verified, operator-authorized mailboxes. No engagement manufacturing or spam bypass."
      />

      <Notice tone="warning" title="Four-state truth">
        A provider message id means accepted. Inbox placement requires you to record the actual
        received headers from a mailbox you control. This console never infers placement.
      </Notice>

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

      <div className="tabs" role="tablist" aria-label="Test sections">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "runs"}
          className={tab === "runs" ? "active" : ""}
          onClick={() => setTab("runs")}
        >
          Test runs
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "allowlist"}
          className={tab === "allowlist" ? "active" : ""}
          onClick={() => setTab("allowlist")}
        >
          Verified allowlist
        </button>
      </div>

      {tab === "runs" && (
        <div className="tab-panel">
          <Card
            title="Start a test run"
            subtitle="Queues ordinary messages with kind=diagnostic_test. The same quota and circuit rules apply."
          >
            <form className="form-grid" onSubmit={createRun}>
              <Field label="Sender account" htmlFor="run_account" required>
                <select
                  id="run_account"
                  value={runForm.account_id}
                  onChange={(event) =>
                    setRunForm((current) => ({ ...current, account_id: event.target.value }))
                  }
                  required
                >
                  <option value="">Select an account</option>
                  {(accounts.data?.accounts || []).map((account) => (
                    <option key={account.id} value={account.id}>
                      {account.mailbox_email} — {account.state}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Profile" htmlFor="run_profile">
                <select
                  id="run_profile"
                  value={runForm.profile}
                  onChange={(event) =>
                    setRunForm((current) => ({ ...current, profile: event.target.value }))
                  }
                >
                  {PROFILES.map((profile) => (
                    <option key={profile.id} value={profile.id}>
                      {profile.label} — {profile.hint}
                    </option>
                  ))}
                </select>
              </Field>
              <Field
                label="Additional allowlisted emails"
                htmlFor="run_emails"
                hint="Comma-separated. Each address must already be on the verified allowlist."
              >
                <input
                  id="run_emails"
                  value={runForm.recipient_emails}
                  onChange={(event) =>
                    setRunForm((current) => ({ ...current, recipient_emails: event.target.value }))
                  }
                  placeholder="ops@example.com"
                />
              </Field>

              <div className="field" style={{ gridColumn: "1 / -1" }}>
                <label>Allowlisted recipients</label>
                {enabledRecipients.length === 0 ? (
                  <p className="muted small">
                    No enabled recipients. Add one on the allowlist tab first.
                  </p>
                ) : (
                  <div className="chip-row">
                    {enabledRecipients.map((recipient) => (
                      <label key={recipient.id} className="badge badge-info" style={{ cursor: "pointer" }}>
                        <input
                          type="checkbox"
                          checked={runForm.recipient_ids.includes(recipient.id)}
                          onChange={(event) => toggleRecipient(recipient.id, event.target.checked)}
                        />
                        {recipient.recipient_email}
                      </label>
                    ))}
                  </div>
                )}
              </div>

              <div className="form-actions">
                <button
                  className="button primary"
                  type="submit"
                  disabled={
                    mutation.busy ||
                    !runForm.account_id ||
                    (runForm.recipient_ids.length === 0 && !runForm.recipient_emails.trim())
                  }
                >
                  {mutation.busy ? "Queueing…" : "Queue test run"}
                </button>
              </div>
            </form>
          </Card>

          <Card
            title="Run history"
            subtitle="Open a run to record observations and parse authentication headers."
            actions={
              <div className="button-row">
                <select
                  aria-label="Filter by account"
                  value={accountId}
                  onChange={(event) => {
                    setAccountId(event.target.value);
                    setOffset(0);
                  }}
                >
                  <option value="">All accounts</option>
                  {(accounts.data?.accounts || []).map((account) => (
                    <option key={account.id} value={account.id}>
                      {account.mailbox_email}
                    </option>
                  ))}
                </select>
                <select
                  aria-label="Filter by status"
                  value={status}
                  onChange={(event) => {
                    setStatus(event.target.value);
                    setOffset(0);
                  }}
                >
                  <option value="">Any status</option>
                  {["queued", "running", "completed", "completed_with_errors", "failed", "cancelled"].map(
                    (value) => (
                      <option key={value} value={value}>
                        {humanize(value)}
                      </option>
                    ),
                  )}
                </select>
              </div>
            }
          >
            {runs.loading && !runs.data ? (
              <Loading />
            ) : (runs.data?.test_runs || []).length === 0 ? (
              <EmptyState title="No test runs yet" />
            ) : (
              <>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Profile</th>
                        <th>Status</th>
                        <th>Recipients</th>
                        <th>Observed</th>
                        <th>Created</th>
                        <th>By</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(runs.data?.test_runs || []).map((run) => (
                        <tr key={run.id}>
                          <td>
                            <Link href={`/tests/${run.id}`}>{humanize(run.test_profile)}</Link>
                          </td>
                          <td>
                            <StatusBadge status={run.status} />
                          </td>
                          <td>{run.results.length}</td>
                          <td>
                            {
                              run.results.filter((result) => result.status === "observed_received")
                                .length
                            }{" "}
                            of {run.results.length}
                          </td>
                          <td>{formatDate(run.created_at)}</td>
                          <td>{run.initiated_by}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="button-row">
                  <button
                    className="button secondary small"
                    type="button"
                    onClick={() => setOffset(Math.max(0, offset - 50))}
                    disabled={offset === 0}
                  >
                    Previous
                  </button>
                  <button
                    className="button secondary small"
                    type="button"
                    onClick={() => setOffset(runs.data?.next_offset ?? offset + 50)}
                    disabled={!runs.data?.has_more}
                  >
                    Next
                  </button>
                </div>
              </>
            )}
          </Card>
        </div>
      )}

      {tab === "allowlist" && (
        <div className="tab-panel">
          <Card
            title="Add a verified recipient"
            subtitle="Only mailboxes you control or are explicitly authorized to test."
          >
            <form className="form-grid" onSubmit={addRecipient}>
              <Field label="Recipient email" htmlFor="recipient_email" required>
                <input
                  id="recipient_email"
                  type="email"
                  value={recipientForm.recipient_email}
                  onChange={(event) =>
                    setRecipientForm((current) => ({
                      ...current,
                      recipient_email: event.target.value,
                    }))
                  }
                  required
                />
              </Field>
              <Field label="Label" htmlFor="recipient_label" hint="Optional, for your own reference.">
                <input
                  id="recipient_label"
                  value={recipientForm.label}
                  onChange={(event) =>
                    setRecipientForm((current) => ({ ...current, label: event.target.value }))
                  }
                  placeholder="My Gmail inbox"
                />
              </Field>
              <Field
                label="Verification method"
                htmlFor="verification_method"
                hint="Challenge-link verification is not implemented in this build."
              >
                <select
                  id="verification_method"
                  value={recipientForm.verification_method}
                  onChange={(event) =>
                    setRecipientForm((current) => ({
                      ...current,
                      verification_method: event.target.value,
                    }))
                  }
                >
                  <option value="operator_confirmed">Operator confirmed</option>
                  <option value="provider_owned_mailbox">Provider-owned mailbox</option>
                </select>
              </Field>
              <div className="field checkbox">
                <input
                  id="attest"
                  type="checkbox"
                  checked={recipientForm.attest}
                  onChange={(event) =>
                    setRecipientForm((current) => ({ ...current, attest: event.target.checked }))
                  }
                />
                <label htmlFor="attest">
                  I confirm I control this mailbox or am explicitly authorized to send diagnostic
                  mail to it.
                </label>
              </div>
              <div className="form-actions">
                <button
                  className="button primary"
                  type="submit"
                  disabled={
                    mutation.busy ||
                    !recipientForm.attest ||
                    !recipientForm.recipient_email.trim()
                  }
                >
                  Add to allowlist
                </button>
              </div>
            </form>
          </Card>

          <Card title="Allowlist" subtitle="Disabling is preferred to deletion when history exists.">
            {recipients.loading && !recipients.data ? (
              <Loading />
            ) : (recipients.data?.recipients || []).length === 0 ? (
              <EmptyState title="No verified recipients yet" />
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Recipient</th>
                      <th>Label</th>
                      <th>Method</th>
                      <th>Verified</th>
                      <th>Enabled</th>
                      <th>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(recipients.data?.recipients || []).map((recipient) => (
                      <tr key={recipient.id}>
                        <td>{recipient.recipient_email}</td>
                        <td>{recipient.label || "—"}</td>
                        <td>{humanize(recipient.verification_method)}</td>
                        <td>{formatRelative(recipient.verified_at)}</td>
                        <td>
                          <Badge tone={recipient.is_enabled ? "success" : "muted"}>
                            {recipient.is_enabled ? "enabled" : "disabled"}
                          </Badge>
                        </td>
                        <td>
                          <div className="button-row">
                            <button
                              className="button secondary small"
                              type="button"
                              onClick={() =>
                                void toggleRecipientState(recipient, !recipient.is_enabled)
                              }
                              disabled={mutation.busy}
                            >
                              {recipient.is_enabled ? "Disable" : "Enable"}
                            </button>
                            <button
                              className="button ghost small"
                              type="button"
                              onClick={() => void deleteRecipient(recipient)}
                              disabled={mutation.busy}
                              style={{ borderColor: "var(--line-strong)", color: "var(--ink)" }}
                            >
                              Delete
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </div>
      )}
    </>
  );
}
