"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { api } from "@/lib/api";
import { formatDate, formatRelative, humanize } from "@/lib/format";
import { useApiData, useMutation } from "@/lib/use-api-data";
import type { DiagnosticReport, MessageDetail } from "@/lib/types";
import {
  Badge,
  Card,
  Code,
  EmptyState,
  Field,
  KeyValue,
  Loading,
  Notice,
  PageHeader,
  StatusBadge,
} from "@/components/ui";

const CANCELLABLE = new Set(["queued", "retry_wait"]);
const REQUEUEABLE = new Set(["failed", "needs_review", "cancelled", "expired"]);

export default function MessageDetailPage() {
  const params = useParams<{ id: string }>();
  const id = typeof params?.id === "string" ? params.id : null;
  const message = useApiData<MessageDetail>(id ? `/messages/${id}` : null);
  const mutation = useMutation();

  const [cancelReason, setCancelReason] = useState("Cancelled by operator from the console");
  const [requeue, setRequeue] = useState({ note: "", confirm: false });
  const [useLlm, setUseLlm] = useState(true);
  const [notice, setNotice] = useState<{ tone: "success" | "danger"; text: string } | null>(null);
  const [freshDiagnosis, setFreshDiagnosis] = useState<DiagnosticReport | null>(null);

  async function cancelMessage() {
    if (!id) return;
    setNotice(null);
    const result = await mutation.run(
      () =>
        api(`/messages/${id}/cancel`, {
          method: "POST",
          body: { reason: cancelReason.trim() },
        }),
      "",
    );
    if (result) {
      setNotice({ tone: "success", text: "Message cancelled. Provider-accepted mail cannot be recalled." });
      message.reload();
    }
  }

  async function requeueMessage() {
    if (!id) return;
    setNotice(null);
    const result = await mutation.run(
      () =>
        api(`/messages/${id}/requeue`, {
          method: "POST",
          body: {
            note: requeue.note.trim(),
            confirm_resubmission_risk: requeue.confirm,
          },
        }),
      "",
    );
    if (result) {
      setNotice({
        tone: "success",
        text: "Message requeued. The operator confirmed the duplicate-submission risk.",
      });
      setRequeue({ note: "", confirm: false });
      message.reload();
    }
  }

  async function diagnose() {
    if (!id) return;
    setNotice(null);
    setFreshDiagnosis(null);
    const result = await mutation.run(
      () =>
        api<{ diagnosis: DiagnosticReport }>(`/messages/${id}/diagnostics`, {
          method: "POST",
          body: { use_llm: useLlm },
        }),
      "",
    );
    if (result) {
      setFreshDiagnosis(result.diagnosis);
      message.reload();
    }
  }

  if (!id) return <Notice tone="danger">Missing message identifier.</Notice>;
  if (message.loading && !message.data) return <Loading label="Loading message…" />;
  if (message.error) {
    return (
      <>
        <PageHeader title="Message" />
        <Notice tone="danger" title="Could not load this message">
          {message.error}
        </Notice>
        <Link className="button secondary" href="/messages">
          Back to message log
        </Link>
      </>
    );
  }
  if (!message.data) return null;

  const data = message.data;
  const canCancel = CANCELLABLE.has(data.status);
  const canRequeue = REQUEUEABLE.has(data.status);

  return (
    <>
      <div className="detail-header">
        <Link className="back-link" href="/messages">
          ← Message log
        </Link>
        <div className="detail-title">
          <h1>{data.subject}</h1>
          <StatusBadge status={data.status} />
          <Badge tone={data.kind === "diagnostic_test" ? "info" : "neutral"}>
            {data.kind.replaceAll("_", " ")}
          </Badge>
          {data.delivery_evidence_at ? (
            <Badge tone="success">delivery evidence</Badge>
          ) : (
            <Badge tone="muted">no delivery evidence</Badge>
          )}
        </div>
        <div className="chip-row">
          <span className="muted small">to {data.to}</span>
          <span className="muted small">from {data.from}</span>
          <span className="muted small">created {formatDate(data.created_at)}</span>
        </div>
      </div>

      <Notice tone="info" title="Reading the lifecycle">
        <strong>submitted</strong> means a provider accepted the message.{" "}
        <strong>delivered</strong> requires provider event evidence. Inbox placement exists only
        after a controlled-test observation with received headers.
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

      <div className="grid two-columns">
        <Card title="Lifecycle" subtitle="Four separate states are never inferred from each other.">
          <KeyValue
            items={[
              { label: "Status", value: <StatusBadge status={data.status} /> },
              { label: "Attempts", value: data.attempt_count },
              {
                label: "Provider message id",
                value: data.provider_message_id ? <code>{data.provider_message_id}</code> : "—",
              },
              { label: "Submitted", value: formatDate(data.submitted_at) },
              { label: "Delivery evidence", value: formatDate(data.delivery_evidence_at) },
              { label: "Next attempt", value: formatDate(data.next_attempt_at) },
              { label: "Scheduled", value: formatDate(data.scheduled_at) },
              { label: "Expires", value: formatDate(data.expires_at) },
              { label: "Idempotency key", value: data.idempotency_key || "—" },
              { label: "Lease", value: data.lease_owner ? `${data.lease_owner} until ${formatDate(data.lease_expires_at)}` : "—" },
            ]}
          />
          {data.final_error_code && (
            <Notice tone="danger" title={data.final_error_code}>
              {data.final_error_summary}
            </Notice>
          )}
          {data.reply_to && <p className="muted small">Reply-To: {data.reply_to}</p>}
          {(data.template_key || data.template_version) && (
            <p className="muted small">
              Template: {data.template_key || "—"} v{data.template_version ?? "—"}
            </p>
          )}
        </Card>

        <Card title="Actions" subtitle="Operator overrides are audited and never silently resubmit.">
          <div className="stack">
            <div className="switch-actions">
              <input
                aria-label="Cancellation reason"
                value={cancelReason}
                onChange={(event) => setCancelReason(event.target.value)}
                minLength={3}
                maxLength={500}
                disabled={!canCancel}
              />
              <button
                className="button secondary"
                type="button"
                onClick={() => void cancelMessage()}
                disabled={!canCancel || mutation.busy || cancelReason.trim().length < 3}
              >
                Cancel message
              </button>
            </div>
            {!canCancel && (
              <p className="muted small">
                Only queued or retry-wait messages can be cancelled. Provider-accepted mail cannot
                be recalled.
              </p>
            )}

            <div className="stack">
              <Field label="Requeue note" htmlFor="requeue_note" required>
                <input
                  id="requeue_note"
                  value={requeue.note}
                  onChange={(event) =>
                    setRequeue((current) => ({ ...current, note: event.target.value }))
                  }
                  placeholder="Why is a second submission acceptable?"
                  minLength={5}
                  maxLength={500}
                />
              </Field>
              <div className="field checkbox">
                <input
                  id="requeue_confirm"
                  type="checkbox"
                  checked={requeue.confirm}
                  onChange={(event) =>
                    setRequeue((current) => ({ ...current, confirm: event.target.checked }))
                  }
                />
                <label htmlFor="requeue_confirm">
                  I understand this may deliver a duplicate and that quota and policy apply again.
                </label>
              </div>
              <button
                className="button danger"
                type="button"
                onClick={() => void requeueMessage()}
                disabled={
                  !canRequeue ||
                  mutation.busy ||
                  !requeue.confirm ||
                  requeue.note.trim().length < 5
                }
              >
                Requeue with confirmation
              </button>
              {!canRequeue && (
                <p className="muted small">
                  Requeue is available for failed, needs-review, cancelled, or expired messages.
                </p>
              )}
            </div>

            <div className="switch-actions">
              <label className="chip-row">
                <input
                  type="checkbox"
                  checked={useLlm}
                  onChange={(event) => setUseLlm(event.target.checked)}
                />
                <span className="small">Allow the configured AI assistant</span>
              </label>
              <button
                className="button primary"
                type="button"
                onClick={() => void diagnose()}
                disabled={mutation.busy}
              >
                {mutation.busy ? "Working…" : "Generate diagnostics"}
              </button>
            </div>
            <p className="muted small">
              Diagnostics are advisory. They never change account state, send messages, or reset a
              circuit.
            </p>
          </div>
        </Card>
      </div>

      <Card title="Message body" subtitle="Stored according to the configured body retention window.">
        <div className="grid two-columns">
          <div>
            <h3 className="small muted">Plain text</h3>
            <div className="message-body">{data.text_body || "—"}</div>
          </div>
          <div>
            <h3 className="small muted">HTML source</h3>
            <div className="message-body">{data.html_body || "—"}</div>
          </div>
        </div>
      </Card>

      <Card title="Delivery attempts" subtitle="Sanitized request/response metadata only. Secrets are never stored here.">
        {data.attempts.length === 0 ? (
          <EmptyState title="No attempts yet">
            The worker has not submitted this message.
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Outcome</th>
                  <th>Provider</th>
                  <th>HTTP</th>
                  <th>SMTP</th>
                  <th>Code</th>
                  <th>Duration</th>
                  <th>Finished</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {data.attempts.map((attempt) => (
                  <tr key={attempt.id}>
                    <td>{attempt.attempt_number}</td>
                    <td>
                      <StatusBadge status={attempt.outcome} />
                    </td>
                    <td>{attempt.provider}</td>
                    <td>{attempt.http_status ?? "—"}</td>
                    <td>
                      {attempt.smtp_code ?? "—"}
                      {attempt.enhanced_smtp_code ? ` / ${attempt.enhanced_smtp_code}` : ""}
                    </td>
                    <td>{attempt.diagnostic_code || "—"}</td>
                    <td>{attempt.duration_ms ? `${attempt.duration_ms} ms` : "—"}</td>
                    <td>{formatDate(attempt.finished_at)}</td>
                    <td className="wrap">
                      {attempt.sanitized_detail || "—"}
                      {Object.keys(attempt.response_metadata || {}).length > 0 && (
                        <details>
                          <summary>Metadata</summary>
                          <Code block>{JSON.stringify(attempt.response_metadata, null, 2)}</Code>
                        </details>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <div className="grid two-columns">
        <Card title="Provider events" subtitle="Delivery, bounce, complaint, and authentication evidence.">
          {data.events.length === 0 ? (
            <EmptyState title="No provider events">
              Send-only API transports may never return delivery events. Acceptance alone is not
              delivery.
            </EmptyState>
          ) : (
            <ul className="timeline">
              {data.events.map((event) => (
                <li key={event.id}>
                  <div className="event-head">
                    <StatusBadge status={event.event_type} />
                    <span className="muted small">{formatDate(event.event_at)}</span>
                  </div>
                  <strong>{event.event_type.replaceAll("_", " ")}</strong>
                  {event.recipient_email && <span>{event.recipient_email}</span>}
                  {event.diagnostic_code && <span>code {event.diagnostic_code}</span>}
                  {event.sanitized_detail && <p>{event.sanitized_detail}</p>}
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="AI diagnostics" subtitle="Advisory interpretation of sanitized evidence.">
          {freshDiagnosis && (
            <DiagnosisCard report={freshDiagnosis} highlight />
          )}
          {data.diagnostics.length === 0 ? (
            <EmptyState title="No diagnostics generated" />
          ) : (
            data.diagnostics.map((report) => <DiagnosisCard key={report.id} report={report} />)
          )}
        </Card>
      </div>
    </>
  );
}

function DiagnosisCard({ report, highlight = false }: { report: DiagnosticReport; highlight?: boolean }) {
  return (
    <div className="stack" style={highlight ? { borderLeft: "3px solid var(--accent)", paddingLeft: "0.75rem" } : undefined}>
      <div className="chip-row">
        <Badge tone="info">{report.engine}</Badge>
        <Badge tone={report.confidence === "high" ? "success" : report.confidence === "low" ? "muted" : "warning"}>
          {report.confidence} confidence
        </Badge>
        {report.needs_operator_action && <Badge tone="warning">operator action</Badge>}
        <span className="muted small">{formatRelative(report.created_at)}</span>
      </div>
      <p>{report.summary}</p>
      {report.likely_causes.length > 0 && (
        <div>
          <strong className="small">Likely causes</strong>
          <ul>
            {report.likely_causes.map((cause) => (
              <li key={cause}>{cause}</li>
            ))}
          </ul>
        </div>
      )}
      {report.recommended_actions.length > 0 && (
        <div>
          <strong className="small">Recommended actions</strong>
          <ul>
            {report.recommended_actions.map((action) => (
              <li key={action}>{action}</li>
            ))}
          </ul>
        </div>
      )}
      <p className="muted small">Advisory only · {humanize(report.engine)}</p>
    </div>
  );
}
