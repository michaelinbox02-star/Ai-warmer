"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApiData, useMutation } from "@/lib/use-api-data";
import type { Account, MessageSummary } from "@/lib/types";
import {
  Badge,
  Card,
  Field,
  Loading,
  Notice,
  PageHeader,
  StatusBadge,
} from "@/components/ui";

export default function ComposeMessagePage() {
  const accounts = useApiData<{ accounts: Account[] }>("/accounts");
  const mutation = useMutation();
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const [queued, setQueued] = useState<{ message: MessageSummary; replayed: boolean } | null>(null);
  const [form, setForm] = useState({
    account_id: "",
    to: "",
    from_email: "",
    reply_to: "",
    subject: "",
    text_body: "",
    html_body: "",
    template_key: "",
    template_version: "",
    expires_at: "",
  });

  useEffect(() => {
    setIdempotencyKey(crypto.randomUUID());
  }, []);

  const selected = (accounts.data?.accounts || []).find(
    (account) => account.id === form.account_id,
  );
  const productionReady =
    selected?.policy?.mode === "production" && selected?.policy?.production_enabled;

  function update(field: keyof typeof form, value: string) {
    setForm((current) => ({ ...current, [field]: value }));
  }

  function resetKey() {
    setIdempotencyKey(crypto.randomUUID());
    setQueued(null);
    mutation.reset();
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!idempotencyKey) return;
    setQueued(null);
    const result = await mutation.run(
      () =>
        api<{ message: MessageSummary; replayed: boolean; note: string }>("/messages", {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey },
          body: {
            account_id: form.account_id,
            to: form.to.trim(),
            from_email: form.from_email.trim() || null,
            reply_to: form.reply_to.trim() || null,
            subject: form.subject,
            text_body: form.text_body.trim() || null,
            html_body: form.html_body.trim() || null,
            template_key: form.template_key.trim() || null,
            template_version: form.template_version
              ? Number(form.template_version)
              : null,
            expires_at: form.expires_at ? new Date(form.expires_at).toISOString() : null,
          },
        }),
      "",
    );
    if (result) {
      setQueued({ message: result.message, replayed: result.replayed });
    }
  }

  return (
    <>
      <PageHeader
        title="Compose transactional message"
        description="Queued mail is submitted by the worker after quota and policy checks. Acceptance is recorded as submitted, never delivered."
        actions={
          <Link className="button secondary" href="/messages">
            Back to log
          </Link>
        }
      />

      <Notice tone="info" title="Idempotency is mandatory">
        Every submission requires an Idempotency-Key header. Retrying with the same key and the same
        payload returns the original message instead of creating a duplicate. Changing the payload
        with the same key is rejected.
      </Notice>

      {queued && (
        <Notice tone="success" title={queued.replayed ? "Existing message returned" : "Message queued"}>
          <p>
            {queued.message.subject} → {queued.message.to}{" "}
            <StatusBadge status={queued.message.status} />
          </p>
          <p className="small">
            <Link href={`/messages/${queued.message.id}`}>Open the message record</Link> · queued{" "}
            {formatDate(queued.message.created_at)}
          </p>
        </Notice>
      )}

      {mutation.error && (
        <Notice tone="danger" title="Could not queue the message">
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

      <Card title="Message" subtitle="Sender addresses must match the connected mailbox or an allowed alias.">
        {accounts.loading && !accounts.data ? (
          <Loading />
        ) : (
          <form className="form-grid" onSubmit={submit}>
            <Field label="Sender account" htmlFor="account_id" required>
              <select
                id="account_id"
                value={form.account_id}
                onChange={(event) => update("account_id", event.target.value)}
                required
              >
                <option value="">Select an account</option>
                {(accounts.data?.accounts || []).map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.mailbox_email} — {account.policy?.mode || "no policy"} ({account.state})
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Recipient" htmlFor="to" required>
              <input
                id="to"
                type="email"
                value={form.to}
                onChange={(event) => update("to", event.target.value)}
                placeholder="customer@example.com"
                required
              />
            </Field>
            <Field
              label="From address"
              htmlFor="from_email"
              hint="Defaults to the mailbox address. Aliases must be pre-approved."
            >
              <input
                id="from_email"
                type="email"
                value={form.from_email}
                onChange={(event) => update("from_email", event.target.value)}
                placeholder={selected?.mailbox_email || "sender@example.com"}
              />
            </Field>
            <Field label="Reply-To" htmlFor="reply_to">
              <input
                id="reply_to"
                type="email"
                value={form.reply_to}
                onChange={(event) => update("reply_to", event.target.value)}
                placeholder="support@example.com"
              />
            </Field>
            <Field label="Subject" htmlFor="subject" required>
              <input
                id="subject"
                value={form.subject}
                onChange={(event) => update("subject", event.target.value)}
                maxLength={998}
                required
              />
            </Field>
            <Field label="Expires at" htmlFor="expires_at" hint="Optional. Expired messages are never retried.">
              <input
                id="expires_at"
                type="datetime-local"
                value={form.expires_at}
                onChange={(event) => update("expires_at", event.target.value)}
              />
            </Field>
            <Field label="Plain-text body" htmlFor="text_body" hint="Provide text, HTML, or both.">
              <textarea
                id="text_body"
                value={form.text_body}
                onChange={(event) => update("text_body", event.target.value)}
                rows={8}
              />
            </Field>
            <Field label="HTML body" htmlFor="html_body">
              <textarea
                id="html_body"
                value={form.html_body}
                onChange={(event) => update("html_body", event.target.value)}
                rows={8}
              />
            </Field>
            <Field label="Template key" htmlFor="template_key" hint="Optional internal reference only.">
              <input
                id="template_key"
                value={form.template_key}
                onChange={(event) => update("template_key", event.target.value)}
              />
            </Field>
            <Field label="Template version" htmlFor="template_version">
              <input
                id="template_version"
                type="number"
                min={1}
                value={form.template_version}
                onChange={(event) => update("template_version", event.target.value)}
              />
            </Field>

            <div className="field" style={{ gridColumn: "1 / -1" }}>
              <label htmlFor="idempotency_key">Idempotency-Key</label>
              <input id="idempotency_key" value={idempotencyKey} readOnly />
              <small>
                Generated in this browser and sent only in the request header. Use the same key to
                replay the same request safely.
              </small>
            </div>

            {selected && !productionReady && (
              <Notice tone="warning">
                This account is not in production mode. Transactional submissions will be rejected
                with <code>account_test_only</code>. Use controlled tests for test-only mailboxes.
              </Notice>
            )}

            <div className="form-actions">
              <button
                className="button primary"
                type="submit"
                disabled={
                  mutation.busy ||
                  !idempotencyKey ||
                  !form.account_id ||
                  !form.to ||
                  !form.subject ||
                  (!form.text_body.trim() && !form.html_body.trim())
                }
              >
                {mutation.busy ? "Queueing…" : "Queue message"}
              </button>
              <button className="button secondary" type="button" onClick={resetKey}>
                New idempotency key
              </button>
              {selected?.policy && (
                <Badge tone={productionReady ? "success" : "warning"}>
                  {selected.policy.mode}
                </Badge>
              )}
            </div>
          </form>
        )}
      </Card>
    </>
  );
}
