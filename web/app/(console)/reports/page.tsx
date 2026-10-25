"use client";

import Link from "next/link";
import { useState } from "react";

import { formatDate, formatRelative, humanize } from "@/lib/format";
import { useApiData } from "@/lib/use-api-data";
import type {
  AuditEntry,
  ReportSummary,
  SafetyEvent,
  SafetyEventSummary,
} from "@/lib/types";
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

type Tab = "overview" | "audit" | "safety";

export default function ReportsPage() {
  const [tab, setTab] = useState<Tab>("overview");
  const [days, setDays] = useState(7);
  const [limit, setLimit] = useState(100);

  const summary = useApiData<ReportSummary>(`/reports/summary?days=${days}`);
  const audit = useApiData<{ entries: AuditEntry[] }>(`/reports/audit?limit=${limit}`);
  const safety = useApiData<{ events: SafetyEvent[] }>(`/reports/safety?limit=${limit}`);

  return (
    <>
      <PageHeader
        title="Reports and audit"
        description="Delivery truth, provider outcomes, circuit state, and an immutable operator trail."
      />

      <div className="tabs" role="tablist" aria-label="Report sections">
        {(
          [
            { id: "overview", label: "Overview" },
            { id: "audit", label: "Audit trail" },
            { id: "safety", label: "Safety events" },
          ] as Array<{ id: Tab; label: string }>
        ).map((item) => (
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
          <Card
            title="Delivery truth"
            subtitle="Provider acceptance, delivery evidence, and failures stay distinct."
            actions={
              <div className="switch-actions">
                <Field label="Period" htmlFor="report_days">
                  <select
                    id="report_days"
                    value={days}
                    onChange={(event) => setDays(Number(event.target.value))}
                  >
                    {[1, 7, 14, 30, 90].map((value) => (
                      <option key={value} value={value}>
                        Last {value} day{value === 1 ? "" : "s"}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
            }
          >
            {summary.loading && !summary.data ? (
              <Loading />
            ) : summary.error ? (
              <Notice tone="danger">{summary.error}</Notice>
            ) : summary.data ? (
              <>
                <div className="truth-grid">
                  <div>
                    <strong>{summary.data.delivery_truth.provider_accepted}</strong>
                    <span>Provider accepted</span>
                  </div>
                  <div>
                    <strong>{summary.data.delivery_truth.delivery_evidence}</strong>
                    <span>Delivery evidence</span>
                  </div>
                  <div>
                    <strong>{summary.data.delivery_truth.awaiting_evidence}</strong>
                    <span>Awaiting evidence</span>
                  </div>
                  <div>
                    <strong>{summary.data.delivery_truth.hard_bounce_or_complaint}</strong>
                    <span>Hard bounce / complaint</span>
                  </div>
                </div>
                <p className="muted small">{summary.data.delivery_truth.note}</p>
                <div className="chip-row">
                  <Badge tone={summary.data.global_sending_enabled ? "success" : "warning"}>
                    global sending {summary.data.global_sending_enabled ? "enabled" : "paused"}
                  </Badge>
                  <span className="muted small">
                    generated {formatRelative(summary.data.generated_at)}
                  </span>
                </div>
              </>
            ) : null}
          </Card>

          {summary.data && (
            <>
              <div className="grid two-columns">
                <Card title="Message states" subtitle="Lifecycle counts in the selected period.">
                  <div className="truth-grid">
                    {Object.entries(summary.data.message_counts).map(([key, value]) => (
                      <div key={key}>
                        <strong>{value}</strong>
                        <span>{humanize(key)}</span>
                      </div>
                    ))}
                  </div>
                </Card>
                <Card title="Attempt outcomes" subtitle="Sanitized provider classifications.">
                  {Object.keys(summary.data.attempt_outcomes).length === 0 ? (
                    <EmptyState title="No attempts yet" />
                  ) : (
                    <div className="truth-grid">
                      {Object.entries(summary.data.attempt_outcomes).map(([key, value]) => (
                        <div key={key}>
                          <strong>{value}</strong>
                          <span>{humanize(key)}</span>
                        </div>
                      ))}
                    </div>
                  )}
                  {Object.keys(summary.data.provider_events).length > 0 && (
                    <>
                      <h3 className="small muted">Provider events</h3>
                      <div className="chip-row">
                        {Object.entries(summary.data.provider_events).map(([key, value]) => (
                          <Badge key={key}>
                            {humanize(key)}: {value}
                          </Badge>
                        ))}
                      </div>
                    </>
                  )}
                </Card>
              </div>

              {summary.data.open_circuits.length > 0 && (
                <Card title="Open circuits" subtitle="Accounts currently blocked from sending.">
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Account</th>
                          <th>State</th>
                          <th>Reason</th>
                          <th>Open until</th>
                        </tr>
                      </thead>
                      <tbody>
                        {summary.data.open_circuits.map((circuit) => (
                          <tr key={circuit.account_id}>
                            <td>
                              <Link href={`/accounts/${circuit.account_id}`}>
                                {circuit.account_id.slice(0, 8)}
                              </Link>
                            </td>
                            <td>
                              <StatusBadge status={circuit.state} />
                            </td>
                            <td className="wrap">{circuit.reason || "—"}</td>
                            <td>{formatDate(circuit.open_until)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </Card>
              )}

              <Card title="Accounts snapshot" subtitle="Readiness, quota, and lifetime counts.">
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Mailbox</th>
                        <th>Connection</th>
                        <th>Mode</th>
                        <th>Circuit</th>
                        <th>Ready</th>
                        <th>24h used</th>
                        <th>Submitted</th>
                        <th>Bounced</th>
                      </tr>
                    </thead>
                    <tbody>
                      {summary.data.accounts.map((account) => (
                        <tr key={account.id}>
                          <td>
                            <Link href={`/accounts/${account.id}`}>{account.mailbox_email}</Link>
                          </td>
                          <td>
                            <StatusBadge status={account.state} />
                          </td>
                          <td>{account.mode ? <StatusBadge status={account.mode} /> : "—"}</td>
                          <td>
                            <StatusBadge status={account.circuit.state || "unknown"} />
                          </td>
                          <td>
                            <Badge tone={account.readiness?.ready ? "success" : "warning"}>
                              {account.readiness?.ready ? "ready" : "blocked"}
                            </Badge>
                          </td>
                          <td>
                            {account.quota
                              ? `${account.quota.rolling_24h_used} / ${account.quota.rolling_24h_limit}`
                              : "—"}
                          </td>
                          <td>{account.lifetime_counts.submitted ?? 0}</td>
                          <td>{account.lifetime_counts.bounced ?? 0}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            </>
          )}
        </div>
      )}

      {tab === "audit" && (
        <Card
          title="Audit trail"
          subtitle="Every operator write, including overrides and acknowledgements."
          actions={
            <select
              aria-label="Audit entry limit"
              value={limit}
              onChange={(event) => setLimit(Number(event.target.value))}
            >
              {[50, 100, 250, 500].map((value) => (
                <option key={value} value={value}>
                  Latest {value}
                </option>
              ))}
            </select>
          }
        >
          {audit.loading && !audit.data ? (
            <Loading />
          ) : audit.error ? (
            <Notice tone="danger">{audit.error}</Notice>
          ) : (audit.data?.entries || []).length === 0 ? (
            <EmptyState title="No audit entries" />
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Actor</th>
                    <th>Action</th>
                    <th>Target</th>
                    <th>Result</th>
                    <th>Request</th>
                    <th>Details</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.data?.entries.map((entry) => (
                    <tr key={entry.id}>
                      <td>{formatDate(entry.created_at)}</td>
                      <td>{entry.actor}</td>
                      <td>
                        <code>{entry.action}</code>
                      </td>
                      <td className="wrap">
                        {entry.target_type || "—"}
                        {entry.target_id && (
                          <div className="muted small">{entry.target_id.slice(0, 8)}</div>
                        )}
                      </td>
                      <td>
                        <StatusBadge status={entry.result} />
                      </td>
                      <td className="wrap">
                        <span className="muted small">{entry.request_id || "—"}</span>
                      </td>
                      <td className="wrap">
                        <details>
                          <summary>View</summary>
                          <Code block>{JSON.stringify(entry.details, null, 2)}</Code>
                        </details>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {tab === "safety" && (
        <div className="tab-panel">
          <Card
            title="Safety events"
            subtitle="Circuit, credential, suppression, and operator override signals."
            actions={
              <select
                aria-label="Safety event limit"
                value={limit}
                onChange={(event) => setLimit(Number(event.target.value))}
              >
                {[50, 100, 250, 500].map((value) => (
                  <option key={value} value={value}>
                    Latest {value}
                  </option>
                ))}
              </select>
            }
          >
            {safety.loading && !safety.data ? (
              <Loading />
            ) : safety.error ? (
              <Notice tone="danger">{safety.error}</Notice>
            ) : (safety.data?.events || []).length === 0 ? (
              <EmptyState title="No safety events" />
            ) : (
              <ul className="event-list">
                {safety.data?.events.map((event) => (
                  <li key={event.id}>
                    <div className="event-head">
                      <StatusBadge status={event.severity} />
                      <strong>{humanize(event.event_type)}</strong>
                      <span className="muted small">{formatDate(event.occurred_at)}</span>
                      {event.account_id && (
                        <Link className="muted small" href={`/accounts/${event.account_id}`}>
                          account
                        </Link>
                      )}
                      {event.message_id && (
                        <Link className="muted small" href={`/messages/${event.message_id}`}>
                          message
                        </Link>
                      )}
                    </div>
                    <p>{event.action_taken}</p>
                    <details>
                      <summary className="small muted">Details</summary>
                      <Code block>{JSON.stringify(event.details, null, 2)}</Code>
                    </details>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          {summary.data && summary.data.recent_safety_events.length > 0 && (
            <Card title="Recent highlights" subtitle="Last events included in the delivery summary.">
              <SummarySafety events={summary.data.recent_safety_events} />
            </Card>
          )}
        </div>
      )}
    </>
  );
}

function SummarySafety({ events }: { events: SafetyEventSummary[] }) {
  return (
    <ul className="event-list">
      {events.map((event) => (
        <li key={event.id}>
          <div className="event-head">
            <StatusBadge status={event.severity} />
            <strong>{humanize(event.event_type)}</strong>
            <span className="muted small">{formatRelative(event.occurred_at)}</span>
          </div>
          <p>{event.action_taken}</p>
        </li>
      ))}
    </ul>
  );
}
