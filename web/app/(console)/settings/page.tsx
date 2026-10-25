"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { bytes, formatDate, formatRelative } from "@/lib/format";
import { useApiData, useMutation } from "@/lib/use-api-data";
import type { SystemStatus } from "@/lib/types";
import {
  Badge,
  Card,
  Code,
  Field,
  KeyValue,
  Loading,
  Notice,
  PageHeader,
  StatusBadge,
} from "@/components/ui";

export default function SettingsPage() {
  const status = useApiData<SystemStatus>("/system/status");
  const mutation = useMutation();
  const [notice, setNotice] = useState<{ tone: "success" | "danger"; text: string } | null>(null);
  const [reason, setReason] = useState("Operator action from settings");
  const [retention, setRetention] = useState({
    body_days: "14",
    event_days: "90",
    audit_days: "365",
  });

  useEffect(() => {
    if (status.data?.retention) {
      setRetention({
        body_days: String(status.data.retention.body_days),
        event_days: String(status.data.retention.event_days),
        audit_days: String(status.data.retention.audit_days),
      });
    }
  }, [status.data?.retention]);

  async function toggleSending() {
    if (!status.data) return;
    setNotice(null);
    const result = await mutation.run(
      () =>
        api(status.data?.global_sending_enabled ? "/system/pause" : "/system/resume", {
          method: "POST",
          body: { reason: reason.trim() },
        }),
      "",
    );
    if (result) {
      setNotice({
        tone: "success",
        text: status.data.global_sending_enabled
          ? "Global sending paused. Already accepted messages cannot be recalled."
          : "Global sending resumed within each account's policy.",
      });
      status.reload();
    }
  }

  async function saveRetention() {
    setNotice(null);
    const result = await mutation.run(
      () =>
        api("/system/retention", {
          method: "POST",
          body: {
            body_days: Number(retention.body_days),
            event_days: Number(retention.event_days),
            audit_days: Number(retention.audit_days),
          },
        }),
      "",
    );
    if (result) {
      setNotice({
        tone: "success",
        text: "Retention updated. Existing records are pruned by the maintenance process; backups age out separately.",
      });
      status.reload();
    }
  }

  if (status.loading && !status.data) return <Loading label="Loading system status…" />;
  if (status.error) {
    return (
      <Notice tone="danger" title="System status unavailable">
        {status.error}
      </Notice>
    );
  }
  if (!status.data) return null;

  const data = status.data;
  const sendingEnabled = data.global_sending_enabled;

  return (
    <>
      <PageHeader
        title="Settings"
        description="Runtime status, quiet safety limits, and explicit retention windows."
      />

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

      <Card
        title="Global safety switch"
        subtitle="Immediate for new submissions. Not a recall mechanism."
        actions={
          <Badge tone={sendingEnabled ? "success" : "warning"}>
            {sendingEnabled ? "sending enabled" : "sending paused"}
          </Badge>
        }
      >
        <div className="switch-panel">
          <p>
            {sendingEnabled
              ? "The worker may submit queued mail within each account's policy, quota, and circuit state."
              : "The worker skips every new submission across all accounts. In-flight submissions finish."}
          </p>
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
          <p className="muted small">
            Every change is audited with the operator identity, reason, request id, and source
            address.
          </p>
        </div>
      </Card>

      <div className="grid two-columns">
        <Card title="Runtime" subtitle="Service health and build identity.">
          <KeyValue
            items={[
              { label: "Version", value: data.version },
              { label: "Environment", value: <Badge>{data.app_env}</Badge> },
              {
                label: "Database",
                value: <StatusBadge status={data.database === "ok" ? "pass" : "fail"} />,
              },
              {
                label: "Secret store",
                value: <StatusBadge status={data.secret_store === "ok" ? "pass" : "fail"} />,
              },
              { label: "Secret backend", value: data.secret_backend },
              {
                label: "AI diagnostics",
                value: data.llm_enabled ? "enabled (advisory only)" : "disabled",
              },
              { label: "Server time", value: formatDate(data.now) },
            ]}
          />
          <p className="muted small">
            The secret store probe verifies reachability without reading or returning any secret.
          </p>
        </Card>

        <Card title="Backlog" subtitle="Queue health as observed by the API.">
          <div className="truth-grid">
            <div>
              <strong>{data.backlog.queued}</strong>
              <span>Queued</span>
            </div>
            <div>
              <strong>{data.backlog.sending}</strong>
              <span>Sending</span>
            </div>
            <div>
              <strong>{data.backlog.needs_review}</strong>
              <span>Needs review</span>
            </div>
            <div>
              <strong>{data.backlog.expired_leases}</strong>
              <span>Expired leases</span>
            </div>
          </div>
          <KeyValue
            items={[
              { label: "Oldest due", value: formatRelative(data.backlog.oldest_due) },
              { label: "Active suppressions", value: data.counts.active_suppressions },
              { label: "Test recipients", value: data.counts.test_recipients },
              {
                label: "API send rate limit",
                value: `${data.limits.send_rate_limit_per_minute} / minute`,
              },
              { label: "Max message size", value: bytes(data.limits.max_message_bytes) },
            ]}
          />
          {data.backlog.expired_leases > 0 && (
            <Notice tone="warning" title="Expired leases present">
              The worker recovers expired leases. Repeated lease expiry may indicate a crash loop or
              a provider call that consistently exceeds the lease window.
            </Notice>
          )}
        </Card>
      </div>

      <Card
        title="Retention"
        subtitle="Message bodies are pruned aggressively; audit records are kept longest."
      >
        <div className="form-grid">
          <Field
            label="Message body retention (days)"
            htmlFor="body_days"
            required
            hint="Bodies and HTML source. Set 1–365."
          >
            <input
              id="body_days"
              type="number"
              min={1}
              max={365}
              value={retention.body_days}
              onChange={(event) =>
                setRetention((current) => ({ ...current, body_days: event.target.value }))
              }
            />
          </Field>
          <Field
            label="Event retention (days)"
            htmlFor="event_days"
            required
            hint="Sanitized provider events and attempt metadata."
          >
            <input
              id="event_days"
              type="number"
              min={1}
              max={3650}
              value={retention.event_days}
              onChange={(event) =>
                setRetention((current) => ({ ...current, event_days: event.target.value }))
              }
            />
          </Field>
          <Field
            label="Audit retention (days)"
            htmlFor="audit_days"
            required
            hint="Operator actions and safety events."
          >
            <input
              id="audit_days"
              type="number"
              min={1}
              max={3650}
              value={retention.audit_days}
              onChange={(event) =>
                setRetention((current) => ({ ...current, audit_days: event.target.value }))
              }
            />
          </Field>
          <div className="form-actions">
            <button
              className="button primary"
              type="button"
              onClick={() => void saveRetention()}
              disabled={mutation.busy}
            >
              Save retention
            </button>
          </div>
        </div>
        <Notice tone="info">
          Backups contain copies of this data and age out on their own schedule. Document the backup
          retention window alongside these settings.
        </Notice>
      </Card>

      <Card title="Provider capabilities" subtitle="Honest transport limits are shown, not inferred.">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Transport</th>
                <th>Send</th>
                <th>Delivery events</th>
                <th>Inbox read</th>
                <th>OAuth</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {data.providers.map((provider) => {
                const capabilities = (provider.capabilities || {}) as Record<string, unknown>;
                const notes = Array.isArray(capabilities.notes)
                  ? (capabilities.notes as string[])
                  : [];
                return (
                  <tr key={String(provider.transport)}>
                    <td>
                      <strong>{String(provider.label || provider.transport)}</strong>
                      <div className="muted small">{String(provider.transport)}</div>
                    </td>
                    <td>
                      <Badge tone={capabilities.send ? "success" : "muted"}>
                        {capabilities.send ? "yes" : "no"}
                      </Badge>
                    </td>
                    <td>
                      <Badge tone={capabilities.delivery_events ? "success" : "muted"}>
                        {capabilities.delivery_events ? "yes" : "no"}
                      </Badge>
                    </td>
                    <td>
                      <Badge tone={capabilities.inbox_read ? "success" : "muted"}>
                        {capabilities.inbox_read ? "yes" : "no"}
                      </Badge>
                    </td>
                    <td>
                      <Badge tone={capabilities.oauth ? "success" : "muted"}>
                        {capabilities.oauth ? "yes" : "no"}
                      </Badge>
                    </td>
                    <td className="wrap">
                      {notes.length > 0 ? (
                        <ul>
                          {notes.map((note) => (
                            <li key={note}>{note}</li>
                          ))}
                        </ul>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="Raw status" subtitle="Full sanitized payload returned by the API.">
        <Code block>{JSON.stringify(data, null, 2)}</Code>
      </Card>
    </>
  );
}
