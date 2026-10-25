"use client";

import Link from "next/link";
import { useState } from "react";

import { query } from "@/lib/api";
import { formatDate, formatRelative } from "@/lib/format";
import { useApiData } from "@/lib/use-api-data";
import type { Account, MessageStatus, MessageSummary } from "@/lib/types";
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

const STATUSES: MessageStatus[] = [
  "queued",
  "sending",
  "retry_wait",
  "submitted",
  "delivered",
  "bounced",
  "complained",
  "failed",
  "needs_review",
  "cancelled",
  "expired",
];

const PAGE_SIZE = 50;

export default function MessagesPage() {
  const [accountId, setAccountId] = useState("");
  const [status, setStatus] = useState("");
  const [kind, setKind] = useState("");
  const [offset, setOffset] = useState(0);

  const accounts = useApiData<{ accounts: Account[] }>("/accounts");
  const path = `/messages${query({
    account_id: accountId,
    status,
    kind,
    limit: PAGE_SIZE,
    offset,
  })}`;
  const messages = useApiData<{
    messages: MessageSummary[];
    has_more: boolean;
    next_offset: number | null;
  }>(path);

  function applyFilter(setter: (value: string) => void, value: string) {
    setter(value);
    setOffset(0);
  }

  const rows = messages.data?.messages || [];

  return (
    <>
      <PageHeader
        title="Messages"
        description="Queued transactional mail and diagnostic tests. Provider acceptance is not delivery."
        actions={
          <Link className="button primary" href="/messages/new">
            Compose
          </Link>
        }
      />

      <Card
        title="Filters"
        subtitle="Narrow by mailbox, lifecycle state, or message kind."
      >
        <div className="form-grid">
          <Field label="Account" htmlFor="filter_account">
            <select
              id="filter_account"
              value={accountId}
              onChange={(event) => applyFilter(setAccountId, event.target.value)}
            >
              <option value="">All accounts</option>
              {(accounts.data?.accounts || []).map((account) => (
                <option key={account.id} value={account.id}>
                  {account.mailbox_email}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Status" htmlFor="filter_status">
            <select
              id="filter_status"
              value={status}
              onChange={(event) => applyFilter(setStatus, event.target.value)}
            >
              <option value="">Any status</option>
              {STATUSES.map((value) => (
                <option key={value} value={value}>
                  {value.replaceAll("_", " ")}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Kind" htmlFor="filter_kind">
            <select
              id="filter_kind"
              value={kind}
              onChange={(event) => applyFilter(setKind, event.target.value)}
            >
              <option value="">Any kind</option>
              <option value="transactional">Transactional</option>
              <option value="diagnostic_test">Diagnostic test</option>
            </select>
          </Field>
        </div>
      </Card>

      <Card
        title="Message log"
        subtitle={`Showing ${rows.length} message${rows.length === 1 ? "" : "s"}${offset ? ` from offset ${offset}` : ""}.`}
      >
        {messages.loading && !messages.data ? (
          <Loading />
        ) : messages.error ? (
          <Notice tone="danger" title="Could not load messages">
            {messages.error}
          </Notice>
        ) : rows.length === 0 ? (
          <EmptyState
            title="No messages match"
            action={
              <Link className="button primary" href="/messages/new">
                Compose the first message
              </Link>
            }
          >
            Transactional messages require an account in production mode. Diagnostic tests are
            created from the controlled test console.
          </EmptyState>
        ) : (
          <>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Recipient</th>
                    <th>Subject</th>
                    <th>Kind</th>
                    <th>Status</th>
                    <th>Attempts</th>
                    <th>Submitted</th>
                    <th>Evidence</th>
                    <th>Created</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((message) => (
                    <tr key={message.id}>
                      <td>
                        <Link href={`/messages/${message.id}`}>{message.to}</Link>
                      </td>
                      <td className="wrap">{message.subject}</td>
                      <td>
                        <Badge tone={message.kind === "diagnostic_test" ? "info" : "neutral"}>
                          {message.kind.replaceAll("_", " ")}
                        </Badge>
                      </td>
                      <td>
                        <StatusBadge status={message.status} />
                      </td>
                      <td>{message.attempt_count}</td>
                      <td>{message.submitted_at ? formatRelative(message.submitted_at) : "—"}</td>
                      <td>
                        {message.delivery_evidence_at ? (
                          <Badge tone="success">yes</Badge>
                        ) : (
                          <Badge tone="muted">awaiting</Badge>
                        )}
                      </td>
                      <td>{formatDate(message.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="button-row">
              <button
                className="button secondary small"
                type="button"
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                disabled={offset === 0}
              >
                Previous
              </button>
              <button
                className="button secondary small"
                type="button"
                onClick={() => setOffset(messages.data?.next_offset ?? offset + PAGE_SIZE)}
                disabled={!messages.data?.has_more}
              >
                Next
              </button>
              <span className="muted small">
                {rows.some((message) => message.status === "needs_review") && (
                  <Badge tone="warning">needs review present</Badge>
                )}
              </span>
            </div>
          </>
        )}
      </Card>
    </>
  );
}
