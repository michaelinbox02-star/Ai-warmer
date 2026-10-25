"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { api } from "@/lib/api";
import { formatDate, humanize } from "@/lib/format";
import { useApiData, useMutation } from "@/lib/use-api-data";
import type { TestRecipient, TestResult, TestRun } from "@/lib/types";
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

export default function TestRunDetailPage() {
  const params = useParams<{ id: string }>();
  const id = typeof params?.id === "string" ? params.id : null;

  const run = useApiData<{ test_run: TestRun }>(id ? `/tests/${id}` : null);
  const recipients = useApiData<{ recipients: TestRecipient[] }>(
    "/tests/recipients?include_disabled=true",
  );
  const mutation = useMutation();

  const [notice, setNotice] = useState<{ tone: "success" | "danger" | "info"; text: string } | null>(
    null,
  );
  const [cancelReason, setCancelReason] = useState("Cancelled by operator from the test console");
  const [observation, setObservation] = useState<{
    resultId: string;
    raw_headers: string;
    observed_inbox: string;
    observed_at: string;
  } | null>(null);

  const recipientById = new Map(
    (recipients.data?.recipients || []).map((recipient) => [recipient.id, recipient]),
  );

  async function refresh() {
    run.reload();
    recipients.reload();
  }

  async function cancelRun() {
    if (!id) return;
    setNotice(null);
    const result = await mutation.run(
      () =>
        api(`/tests/${id}/cancel`, {
          method: "POST",
          body: { reason: cancelReason.trim() },
        }),
      "",
    );
    if (result) {
      setNotice({
        tone: "success",
        text: "Queued tests were cancelled. Provider-accepted mail cannot be recalled.",
      });
      refresh();
    }
  }

  async function submitObservation(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!id || !observation) return;
    setNotice(null);
    const result = await mutation.run(
      () =>
        api(`/tests/${id}/results/${observation.resultId}/observation`, {
          method: "POST",
          body: {
            raw_headers: observation.raw_headers,
            observed_inbox: observation.observed_inbox.trim() || null,
            observed_at: observation.observed_at
              ? new Date(observation.observed_at).toISOString()
              : null,
          },
        }),
      "",
    );
    if (result) {
      setNotice({
        tone: "success",
        text: "Observation recorded. Authentication verdicts come from the supplied headers.",
      });
      setObservation(null);
      refresh();
    }
  }

  if (!id) return <Notice tone="danger">Missing test run identifier.</Notice>;
  if (run.loading && !run.data) return <Loading label="Loading test run…" />;
  if (run.error) {
    return (
      <>
        <PageHeader title="Test run" />
        <Notice tone="danger" title="Could not load this test run">
          {run.error}
        </Notice>
        <Link className="button secondary" href="/tests">
          Back to controlled tests
        </Link>
      </>
    );
  }
  const data = run.data?.test_run;
  if (!data) return null;

  const cancelled = data.status === "cancelled";
  const observedCount = data.results.filter(
    (result) => result.status === "observed_received" || result.status === "auth_pass" || result.status === "warning",
  ).length;

  return (
    <>
      <div className="detail-header">
        <Link className="back-link" href="/tests">
          ← Controlled tests
        </Link>
        <div className="detail-title">
          <h1>{humanize(data.test_profile)}</h1>
          <StatusBadge status={data.status} />
          <Badge tone={observedCount === data.results.length && data.results.length > 0 ? "success" : "info"}>
            {observedCount} / {data.results.length} observed
          </Badge>
        </div>
        <div className="chip-row">
          <span className="muted small">run {data.id.slice(0, 8)}</span>
          <span className="muted small">created {formatDate(data.created_at)}</span>
          <span className="muted small">by {data.initiated_by}</span>
          <Link className="muted small" href={`/accounts/${data.account_id}`}>
            open account
          </Link>
        </div>
      </div>

      <Notice tone="info" title="How placement is established">
        Submission only proves provider acceptance. To establish inbox placement, open the message
        in the controlled mailbox, copy its received headers, and record them per recipient below.
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
        <Card title="Run summary">
          <KeyValue
            items={[
              { label: "Status", value: <StatusBadge status={data.status} /> },
              { label: "Profile", value: humanize(data.test_profile) },
              { label: "Started", value: formatDate(data.started_at) },
              { label: "Completed", value: formatDate(data.completed_at) },
              { label: "Recipients", value: data.results.length },
              { label: "Observed", value: observedCount },
            ]}
          />
          {Object.keys(data.summary || {}).length > 0 && (
            <Code block>{JSON.stringify(data.summary, null, 2)}</Code>
          )}
        </Card>

        <Card title="Cancel remaining tests" subtitle="Cancels queued messages only; accepted mail cannot be recalled.">
          <div className="switch-actions">
            <input
              aria-label="Cancellation reason"
              value={cancelReason}
              onChange={(event) => setCancelReason(event.target.value)}
              minLength={3}
              maxLength={500}
              disabled={cancelled}
            />
            <button
              className="button danger"
              type="button"
              onClick={() => void cancelRun()}
              disabled={cancelled || mutation.busy || cancelReason.trim().length < 3}
            >
              Cancel run
            </button>
          </div>
          {cancelled && <p className="muted small">This run is already cancelled.</p>}
          <p className="muted small">
            Cancellation marks pending results as timed out and stops queued submissions.
          </p>
        </Card>
      </div>

      <Card title="Recipients and observations" subtitle="Raw headers are parsed locally; nothing is inferred from acceptance.">
        {data.results.length === 0 ? (
          <EmptyState title="No results recorded for this run" />
        ) : (
          <div className="stack">
            {data.results.map((result) => (
              <ResultBlock
                key={result.id}
                result={result}
                recipientEmail={recipientById.get(result.allowed_recipient_id)?.recipient_email}
                disabled={cancelled}
                editing={observation?.resultId === result.id}
                onEdit={() =>
                  setObservation({
                    resultId: result.id,
                    raw_headers: result.authentication_results?.raw || "",
                    observed_inbox: result.observed_inbox || "",
                    observed_at: "",
                  })
                }
                onCancelEdit={() => setObservation(null)}
                form={observation?.resultId === result.id ? observation : null}
                setForm={setObservation}
                onSubmit={submitObservation}
                busy={mutation.busy}
              />
            ))}
          </div>
        )}
      </Card>
    </>
  );
}

function ResultBlock({
  result,
  recipientEmail,
  disabled,
  editing,
  onEdit,
  onCancelEdit,
  form,
  setForm,
  onSubmit,
  busy,
}: {
  result: TestResult;
  recipientEmail?: string;
  disabled: boolean;
  editing: boolean;
  onEdit: () => void;
  onCancelEdit: () => void;
  form: { resultId: string; raw_headers: string; observed_inbox: string; observed_at: string } | null;
  setForm: (
    value: { resultId: string; raw_headers: string; observed_inbox: string; observed_at: string } | null,
  ) => void;
  onSubmit: (event: React.FormEvent<HTMLFormElement>) => void;
  busy: boolean;
}) {
  const auth = result.authentication_results || {};
  const mechanisms = auth.mechanisms || {};

  return (
    <div className="card" style={{ boxShadow: "none" }}>
      <div className="card-header">
        <div>
          <div className="chip-row">
            <strong>{recipientEmail || result.allowed_recipient_id.slice(0, 8)}</strong>
            <StatusBadge status={result.status} />
            {result.observed_inbox && <Badge tone="info">inbox: {result.observed_inbox}</Badge>}
            {result.latency_ms !== null && (
              <span className="muted small">{Math.round(result.latency_ms / 1000)}s latency</span>
            )}
          </div>
          <p className="card-subtitle">
            {result.message_id ? (
              <Link href={`/messages/${result.message_id}`}>Open message record</Link>
            ) : (
              "No message linked"
            )}{" "}
            · observed {formatDate(result.observed_at)}
          </p>
        </div>
        <div className="card-actions">
          {!disabled && !editing && (
            <button className="button secondary small" type="button" onClick={onEdit}>
              Record observation
            </button>
          )}
        </div>
      </div>

      {result.result_detail && <p className="small">{result.result_detail}</p>}

      {Object.keys(mechanisms).length > 0 && (
        <div className="chip-row">
          {Object.entries(mechanisms).map(([name, mechanism]) => (
            <Badge key={name} tone={mechanism.result === "pass" ? "success" : "warning"}>
              {name}: {mechanism.result}
            </Badge>
          ))}
        </div>
      )}

      {auth.note && <p className="muted small">{auth.note}</p>}

      {auth.raw && (
        <details>
          <summary className="small">Authentication-Results</summary>
          <Code block>{auth.raw}</Code>
        </details>
      )}

      {editing && form && (
        <form className="form-grid" onSubmit={onSubmit}>
          <Field
            label="Received headers"
            htmlFor={`headers_${result.id}`}
            required
            hint="Paste the message headers from the controlled mailbox. At least 10 characters."
          >
            <textarea
              id={`headers_${result.id}`}
              value={form.raw_headers}
              onChange={(event) =>
                setForm({ ...form, raw_headers: event.target.value })
              }
              rows={8}
              minLength={10}
              required
            />
          </Field>
          <Field label="Observed inbox" htmlFor={`inbox_${result.id}`} hint="e.g. Inbox, Spam, Promotions">
            <input
              id={`inbox_${result.id}`}
              value={form.observed_inbox}
              onChange={(event) =>
                setForm({ ...form, observed_inbox: event.target.value })
              }
              maxLength={320}
            />
          </Field>
          <Field label="Observed at" htmlFor={`observed_${result.id}`}>
            <input
              id={`observed_${result.id}`}
              type="datetime-local"
              value={form.observed_at}
              onChange={(event) => setForm({ ...form, observed_at: event.target.value })}
            />
          </Field>
          <div className="form-actions">
            <button
              className="button primary"
              type="submit"
              disabled={busy || form.raw_headers.trim().length < 10}
            >
              Save observation
            </button>
            <button className="button secondary" type="button" onClick={onCancelEdit} disabled={busy}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
