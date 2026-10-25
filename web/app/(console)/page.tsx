"use client";

import Link from "next/link";
import { useState } from "react";

import { api } from "@/lib/api";
import { formatRelative } from "@/lib/format";
import { useApiData, useMutation } from "@/lib/use-api-data";
import type { ReportSummary, SystemStatus } from "@/lib/types";
import {
  Badge,
  Card,
  EmptyState,
  Loading,
  Meter,
  Notice,
  PageHeader,
  StatusBadge,
} from "@/components/ui";

const METRICS: Array<{ key: string; label: string; tone?: string; hint: string }> = [
  { key: "queued", label: "Queued", hint: "Waiting for the worker" },
  { key: "sending", label: "Sending", hint: "Leased right now" },
  { key: "submitted", label: "Accepted", tone: "info", hint: "Provider accepted; not delivery" },
  { key: "delivered", label: "Delivery evidence", tone: "success", hint: "Event or observation" },
  { key: "bounced", label: "Bounced", tone: "danger", hint: "Hard bounce evidence" },
  { key: "complained", label: "Complaints", tone: "danger", hint: "Suppressed immediately" },
  { key: "failed", label: "Failed", tone: "danger", hint: "Terminal failure" },
  { key: "needs_review", label: "Needs review", tone: "warning", hint: "Ambiguous result" },
];

export default function DashboardPage() {
  const summary = useApiData<ReportSummary>("/reports/summary?days=7");
  const status = useApiData<SystemStatus>("/system/status");
  const mutation = useMutation();
  const [reason, setReason] = useState("Operator action from dashboard");

  const sendingEnabled = summary.data?.global_sending_enabled ?? status.data?.global_sending_enabled;

  async function toggleSending() {
    if (sendingEnabled === undefined) return;
    const result = await mutation.run(
      () =>
        api(sendingEnabled ? "/system/pause" : "/system/resume", {
          method: "POST",
          body: { reason },
        }),
      sendingEnabled ? "Global sending paused." : "Global sending resumed.",
    );
    if (result) {
      summary.reload();
      status.reload();
    }
  }

  if (summary.loading && !summary.data) return <Loading label="Loading delivery overview…" />;
  if (summary.error) {
    return (
      <Notice tone="danger" title="Dashboard unavailable">
        {summary.error}
      </Notice>
    );
  }
  if (!summary.data) return null;

  const data = summary.data;
  const accounts = data.accounts;

  return (
    <>
      <PageHeader
        title="Delivery overview"
        description="Four states stay separate: connection, provider acceptance, delivery evidence, and inbox placement."
        actions={
          <Link className="button primary" href="/messages">
            Compose a message
          </Link>
        }
      />

      {sendingEnabled === false && (
        <Notice tone="warning" title="Global sending is paused">
          Future submissions are blocked. Messages already accepted by a provider cannot be
          recalled or unsent.
        </Notice>
      )}

      {data.delivery_truth.awaiting_evidence > 0 && (
        <Notice tone="info" title="Accepted is not delivered">
          {data.delivery_truth.awaiting_evidence} accepted submission
          {data.delivery_truth.awaiting_evidence === 1 ? "" : "s"} still lack delivery evidence.
          This is expected for send-only API transports. Use controlled-mailbox observations or
          provider events where available.
        </Notice>
      )}

      <div className="stat-grid">
        {METRICS.map((metric) => (
          <div className={`stat ${metric.tone ? `stat-${metric.tone}` : ""}`} key={metric.key}>
            <span className="stat-label">{metric.label}</span>
            <strong>{data.message_counts[metric.key] ?? 0}</strong>
            <small>{metric.hint}</small>
          </div>
        ))}
      </div>

      <div className="grid two-columns">
        <Card
          title="Global safety switch"
          subtitle="Blocks every future submission across all accounts."
          actions={
            <Badge tone={sendingEnabled ? "success" : "warning"}>
              {sendingEnabled ? "sending enabled" : "sending paused"}
            </Badge>
          }
        >
          <div className="switch-panel">
            <div>
              <p>
                {sendingEnabled
                  ? "The worker may submit queued mail within each account's policy."
                  : "The worker skips all new submissions. Existing leases finish only if already in flight."}
              </p>
              <p className="muted small">
                Pausing is immediate for new work. It is not a recall mechanism.
              </p>
            </div>
            <div className="switch-actions">
              <input
                aria-label="Reason for changing the global switch"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                minLength={3}
                maxLength={500}
              />
              <button
                className={`button ${sendingEnabled ? "danger" : "primary"}`}
                type="button"
                onClick={() => void toggleSending()}
                disabled={mutation.busy || reason.trim().length < 3}
              >
                {mutation.busy
                  ? "Applying…"
                  : sendingEnabled
                    ? "Pause all sending"
                    : "Resume sending"}
              </button>
            </div>
          </div>
          {mutation.error && <Notice tone="danger">{mutation.error}</Notice>}
          {mutation.message && <Notice tone="success">{mutation.message}</Notice>}
        </Card>

        <Card title="Runtime" subtitle="Local service and queue health">
          {status.loading && !status.data ? (
            <Loading />
          ) : status.data ? (
            <div className="runtime-grid">
              <div>
                <span className="muted small">Database</span>
                <StatusBadge status={status.data.database === "ok" ? "pass" : "fail"} />
              </div>
              <div>
                <span className="muted small">Secret store</span>
                <StatusBadge status={status.data.secret_store === "ok" ? "pass" : "fail"} />
              </div>
              <div>
                <span className="muted small">Environment</span>
                <strong>{status.data.app_env}</strong>
              </div>
              <div>
                <span className="muted small">Backlog</span>
                <strong>{status.data.backlog.queued} queued</strong>
              </div>
              <div>
                <span className="muted small">Expired leases</span>
                <strong>{status.data.backlog.expired_leases}</strong>
              </div>
              <div>
                <span className="muted small">AI diagnostics</span>
                <strong>{status.data.llm_enabled ? "enabled (advisory)" : "off"}</strong>
              </div>
            </div>
          ) : (
            <Notice tone="danger">{status.error}</Notice>
          )}
        </Card>
      </div>

      <Card
        title="Accounts"
        subtitle={`${accounts.length} of 5 mailbox slots configured`}
        actions={
          <Link className="button secondary small" href="/accounts">
            Manage accounts
          </Link>
        }
      >
        {accounts.length === 0 ? (
          <EmptyState
            title="No sender accounts yet"
            action={
              <Link className="button primary" href="/accounts">
                Connect the first account
              </Link>
            }
          >
            Connect Gmail, Microsoft 365/Outlook, or a custom SMTP mailbox to begin.
          </EmptyState>
        ) : (
          <div className="account-list">
            {accounts.map((account) => {
              const readiness = account.readiness;
              return (
                <Link href={`/accounts/${account.id}`} className="account-row" key={account.id}>
                  <div className="account-main">
                    <div className="account-title">
                      <strong>{account.mailbox_email}</strong>
                      <StatusBadge status={account.state} />
                      {account.mode && <StatusBadge status={account.mode} />}
                      {account.circuit.state && account.circuit.state !== "closed" && (
                        <StatusBadge status={account.circuit.state} label="circuit open" />
                      )}
                    </div>
                    <div className="account-meta">
                      <span>{account.provider.replaceAll("_", " ")}</span>
                      <span>{account.transport.replaceAll("_", " ")}</span>
                      <span>{account.sending_domain || "domain not set"}</span>
                    </div>
                    {account.last_error_summary && (
                      <p className="inline-error">
                        {account.last_error_code}: {account.last_error_summary}
                      </p>
                    )}
                    {readiness && !readiness.ready && (
                      <p className="muted small">
                        Blocked by: {readiness.blocking.map((item) => item.replaceAll("_", " ")).join(", ")}
                      </p>
                    )}
                  </div>
                  <div className="account-side">
                    <Badge tone={readiness?.ready ? "success" : "warning"}>
                      {readiness?.ready ? "production ready" : "not ready"}
                    </Badge>
                    {account.quota && (
                      <Meter
                        used={account.quota.rolling_24h_used}
                        limit={account.quota.rolling_24h_limit}
                        label="rolling 24h"
                      />
                    )}
                    <span className="muted small">
                      tested {formatRelative(account.connection_tested_at)}
                    </span>
                  </div>
                </Link>
              );
            })}
          </div>
        )}
      </Card>

      <div className="grid two-columns">
        <Card title="Recent safety events" subtitle="Circuit, policy, and credential signals">
          {data.recent_safety_events.length === 0 ? (
            <EmptyState title="No safety events recorded" />
          ) : (
            <ul className="event-list">
              {data.recent_safety_events.map((event) => (
                <li key={event.id}>
                  <div className="event-head">
                    <StatusBadge status={event.severity} />
                    <strong>{event.event_type.replaceAll("_", " ")}</strong>
                    <span className="muted small">{formatRelative(event.occurred_at)}</span>
                  </div>
                  <p>{event.action_taken}</p>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Delivery truth" subtitle={`Last ${data.period_days} days`}>
          <div className="truth-grid">
            <div>
              <strong>{data.delivery_truth.provider_accepted}</strong>
              <span>Provider accepted</span>
            </div>
            <div>
              <strong>{data.delivery_truth.delivery_evidence}</strong>
              <span>Delivery evidence</span>
            </div>
            <div>
              <strong>{data.delivery_truth.awaiting_evidence}</strong>
              <span>Awaiting evidence</span>
            </div>
            <div>
              <strong>{data.delivery_truth.hard_bounce_or_complaint}</strong>
              <span>Bounce / complaint</span>
            </div>
          </div>
          <p className="muted small">{data.delivery_truth.note}</p>
          <p className="muted small">
            Inbox placement appears only in controlled tests after received headers are recorded.
          </p>
        </Card>
      </div>
    </>
  );
}
