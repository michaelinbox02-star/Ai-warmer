"use client";

import { useState } from "react";

import { api } from "@/lib/api";
import { formatDate, humanize } from "@/lib/format";
import { useApiData, useMutation } from "@/lib/use-api-data";
import type { Suppression } from "@/lib/types";
import {
  Badge,
  Card,
  Code,
  EmptyState,
  Field,
  Loading,
  Notice,
  PageHeader,
  StatusBadge,
} from "@/components/ui";

export default function SuppressionsPage() {
  const [activeOnly, setActiveOnly] = useState(true);
  const [limit, setLimit] = useState(200);
  const suppressions = useApiData<{ suppressions: Suppression[] }>(
    `/suppressions?active_only=${activeOnly}&limit=${limit}`,
  );
  const mutation = useMutation();
  const [notice, setNotice] = useState<{ tone: "success" | "danger"; text: string } | null>(null);
  const [removal, setRemoval] = useState({ id: "", reason: "" });

  async function removeSuppression() {
    if (!removal.id) return;
    setNotice(null);
    const result = await mutation.run(
      () =>
        api(`/suppressions/${removal.id}`, {
          method: "DELETE",
          body: { reason: removal.reason.trim() },
        }),
      "",
    );
    if (result !== null) {
      setNotice({
        tone: "success",
        text: "Suppression removed and audited. Sending to this recipient is allowed again immediately.",
      });
      setRemoval({ id: "", reason: "" });
      suppressions.reload();
    }
  }

  const rows = suppressions.data?.suppressions || [];
  const active = rows.filter((row) => !row.removed_at);

  return (
    <>
      <PageHeader
        title="Suppressions"
        description="Hard bounces, complaints, and operator suppressions block every connected account."
      />

      <Notice tone="warning" title="Removal is a deliberate override">
        Suppressed recipients cannot be mailed from any account. Removing an entry requires a
        reason and is recorded in the audit trail. Only remove a suppression when you have
        independent evidence that the address is safe to contact.
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

      <Card
        title={`Suppression list (${active.length} active)`}
        subtitle="Active suppressions are enforced by the worker before any provider call."
        actions={
          <div className="button-row">
            <label className="chip-row small">
              <input
                type="checkbox"
                checked={activeOnly}
                onChange={(event) => setActiveOnly(event.target.checked)}
              />
              Active only
            </label>
            <select
              aria-label="Suppression limit"
              value={limit}
              onChange={(event) => setLimit(Number(event.target.value))}
            >
              {[100, 200, 500].map((value) => (
                <option key={value} value={value}>
                  Latest {value}
                </option>
              ))}
            </select>
          </div>
        }
      >
        {suppressions.loading && !suppressions.data ? (
          <Loading />
        ) : suppressions.error ? (
          <Notice tone="danger">{suppressions.error}</Notice>
        ) : rows.length === 0 ? (
          <EmptyState title={activeOnly ? "No active suppressions" : "No suppressions recorded"}>
            Hard bounces and complaints from provider events are added automatically. Operators can
            also add suppressions through the event intake with an audited reason.
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Recipient</th>
                  <th>Reason</th>
                  <th>Source</th>
                  <th>Created</th>
                  <th>State</th>
                  <th>Removal</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td>{row.recipient_email}</td>
                    <td>
                      <Badge tone={row.reason === "complaint" || row.reason === "hard_bounce" ? "danger" : "warning"}>
                        {humanize(row.reason)}
                      </Badge>
                    </td>
                    <td className="wrap">
                      {row.source_account_id ? (
                        <span className="muted small">account {row.source_account_id.slice(0, 8)}</span>
                      ) : (
                        "—"
                      )}
                      {row.source_message_id && (
                        <div className="muted small">message {row.source_message_id.slice(0, 8)}</div>
                      )}
                    </td>
                    <td>{formatDate(row.created_at)}</td>
                    <td>
                      {row.removed_at ? (
                        <Badge tone="muted">removed</Badge>
                      ) : (
                        <StatusBadge status="active" label="active" />
                      )}
                      {row.removed_at && (
                        <div className="muted small">{formatDate(row.removed_at)}</div>
                      )}
                    </td>
                    <td className="wrap">
                      {row.removed_by || "—"}
                      {row.removal_reason && (
                        <div className="muted small">{row.removal_reason}</div>
                      )}
                    </td>
                    <td>
                      {!row.removed_at && (
                        <button
                          className="button secondary small"
                          type="button"
                          onClick={() => setRemoval({ id: row.id, reason: "" })}
                        >
                          Remove
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {removal.id && (
        <Card
          title="Remove suppression"
          subtitle="This override is audited and takes effect immediately."
        >
          <div className="form-grid">
            <Field
              label="Reason"
              htmlFor="removal_reason"
              required
              hint="At least 3 characters. Explain the independent evidence."
            >
              <input
                id="removal_reason"
                value={removal.reason}
                onChange={(event) => setRemoval((current) => ({ ...current, reason: event.target.value }))}
                placeholder="Recipient confirmed the mailbox and requested further mail"
                minLength={3}
                maxLength={500}
              />
            </Field>
            <div className="form-actions">
              <button
                className="button danger"
                type="button"
                onClick={() => void removeSuppression()}
                disabled={mutation.busy || removal.reason.trim().length < 3}
              >
                Remove and audit
              </button>
              <button
                className="button secondary"
                type="button"
                onClick={() => setRemoval({ id: "", reason: "" })}
                disabled={mutation.busy}
              >
                Cancel
              </button>
            </div>
          </div>
          <Code block>
            {JSON.stringify(
              rows.find((row) => row.id === removal.id) || { id: removal.id },
              null,
              2,
            )}
          </Code>
        </Card>
      )}
    </>
  );
}
