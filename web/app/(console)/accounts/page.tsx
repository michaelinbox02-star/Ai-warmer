"use client";

import Link from "next/link";
import { useState } from "react";

import { api, errorMessage } from "@/lib/api";
import { formatRelative } from "@/lib/format";
import { useApiData, useMutation } from "@/lib/use-api-data";
import type { Account } from "@/lib/types";
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

interface AccountsResponse {
  accounts: Account[];
}

const OAUTH_PROVIDERS = [
  { id: "google_gmail", label: "Gmail", hint: "Google personal mailbox" },
  { id: "google_workspace", label: "Google Workspace", hint: "Workspace mailbox" },
  { id: "microsoft_365", label: "Microsoft 365", hint: "Business mailbox" },
  { id: "outlook_com", label: "Outlook.com", hint: "Personal Microsoft mailbox" },
];

export default function AccountsPage() {
  const accounts = useApiData<AccountsResponse>("/accounts");
  const mutation = useMutation();
  const [oauthBusy, setOauthBusy] = useState<string | null>(null);
  const [oauthError, setOauthError] = useState("");
  const [form, setForm] = useState({
    mailbox_email: "",
    display_name: "",
    sending_domain: "",
    host: "",
    port: "587",
    security: "starttls",
    username: "",
    password: "",
    dkim_selectors: "",
  });

  function update(field: keyof typeof form, value: string) {
    setForm((current) => ({ ...current, [field]: value }));
  }

  async function startOAuth(provider: string) {
    setOauthBusy(provider);
    setOauthError("");
    try {
      const result = await api<{ authorize_url: string }>(
        `/accounts/oauth/${provider}/start`,
        { method: "POST" },
      );
      window.location.assign(result.authorize_url);
    } catch (err) {
      setOauthError(errorMessage(err));
      setOauthBusy(null);
    }
  }

  async function createSmtp(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const result = await mutation.run(
      () =>
        api<Account>("/accounts", {
          method: "POST",
          body: {
            provider: "custom_smtp",
            transport: "smtp",
            mailbox_email: form.mailbox_email.trim(),
            display_name: form.display_name.trim() || null,
            sending_domain: form.sending_domain.trim() || null,
            smtp: {
              host: form.host.trim(),
              port: Number(form.port),
              security: form.security,
              username: form.username.trim() || null,
              password: form.password,
            },
            dkim_selectors: form.dkim_selectors
              .split(",")
              .map((value) => value.trim())
              .filter(Boolean),
          },
        }),
      "SMTP account saved. Run a connection test before anything else.",
    );
    if (result) {
      setForm({
        mailbox_email: "",
        display_name: "",
        sending_domain: "",
        host: "",
        port: "587",
        security: "starttls",
        username: "",
        password: "",
        dkim_selectors: "",
      });
      accounts.reload();
    }
  }

  const rows = accounts.data?.accounts || [];
  const atCapacity = rows.length >= 5;

  return (
    <>
      <PageHeader
        title="Sender accounts"
        description="Up to five mailboxes. New accounts start in test-only mode with production disabled."
      />

      {accounts.loading && !accounts.data ? (
        <Loading />
      ) : accounts.error ? (
        <Notice tone="danger" title="Could not load accounts">
          {accounts.error}
        </Notice>
      ) : (
        <Card
          title={`Connected mailboxes (${rows.length} / 5)`}
          subtitle="Connection state, mode, and readiness are independent."
        >
          {rows.length === 0 ? (
            <EmptyState title="No accounts configured">
              Use one of the connection methods below. Start with the mailbox you actually send
              transactional mail from.
            </EmptyState>
          ) : (
            <div className="account-grid">
              {rows.map((account) => (
                <Link className="account-tile" href={`/accounts/${account.id}`} key={account.id}>
                  <div className="account-tile-head">
                    <strong>{account.mailbox_email}</strong>
                    <StatusBadge status={account.state} />
                  </div>
                  <p>{account.display_name || account.sending_domain || "No display name"}</p>
                  <div className="chip-row">
                    <Badge>{account.provider.replaceAll("_", " ")}</Badge>
                    <Badge>{account.transport.replaceAll("_", " ")}</Badge>
                    {account.policy && <StatusBadge status={account.policy.mode} />}
                    {account.readiness && (
                      <Badge tone={account.readiness.ready ? "success" : "warning"}>
                        {account.readiness.ready ? "ready" : "not ready"}
                      </Badge>
                    )}
                  </div>
                  <div className="account-tile-foot">
                    <span>{account.credentials ? `secret v${account.credentials.credential_version}` : "no secret"}</span>
                    <span>tested {formatRelative(account.connection_tested_at)}</span>
                  </div>
                  {account.last_error_code && (
                    <p className="inline-error">
                      {account.last_error_code}: {account.last_error_summary}
                    </p>
                  )}
                </Link>
              ))}
            </div>
          )}
        </Card>
      )}

      <div className="grid two-columns">
        <Card
          title="Connect with OAuth"
          subtitle="Preferred for Gmail and Microsoft. Send-only least-privilege scopes."
        >
          {atCapacity && (
            <Notice tone="warning">
              The five-mailbox limit is reached. Disable or delete an account first.
            </Notice>
          )}
          {oauthError && <Notice tone="danger">{oauthError}</Notice>}
          <div className="oauth-grid">
            {OAUTH_PROVIDERS.map((provider) => (
              <button
                key={provider.id}
                type="button"
                className="oauth-button"
                disabled={Boolean(oauthBusy) || atCapacity}
                onClick={() => void startOAuth(provider.id)}
              >
                <strong>{oauthBusy === provider.id ? "Redirecting…" : provider.label}</strong>
                <span>{provider.hint}</span>
              </button>
            ))}
          </div>
          <p className="muted small">
            The redirect uses PKCE and single-use state. Refresh tokens are written to the
            configured secret store, never PostgreSQL. You must complete the provider consent
            screen in this browser.
          </p>
        </Card>

        <Card
          title="Custom authenticated SMTP"
          subtitle="TLS is mandatory. Private, loopback, and metadata destinations are blocked."
        >
          {atCapacity && <Notice tone="warning">Five-mailbox limit reached.</Notice>}
          {mutation.error && (
            <Notice tone="danger" title="Could not save SMTP account">
              {mutation.error}
              {mutation.issues.length > 0 && (
                <ul>
                  {mutation.issues.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              )}
            </Notice>
          )}
          {mutation.message && <Notice tone="success">{mutation.message}</Notice>}
          <form onSubmit={createSmtp} className="form-grid">
            <Field label="Mailbox email" htmlFor="mailbox_email" required>
              <input
                id="mailbox_email"
                type="email"
                value={form.mailbox_email}
                onChange={(event) => update("mailbox_email", event.target.value)}
                placeholder="sender@example.com"
                required
              />
            </Field>
            <Field label="Display name" htmlFor="display_name">
              <input
                id="display_name"
                value={form.display_name}
                onChange={(event) => update("display_name", event.target.value)}
                placeholder="Example Service"
              />
            </Field>
            <Field label="Sending domain" htmlFor="sending_domain" hint="Defaults to the mailbox domain.">
              <input
                id="sending_domain"
                value={form.sending_domain}
                onChange={(event) => update("sending_domain", event.target.value)}
                placeholder="example.com"
              />
            </Field>
            <Field label="SMTP host" htmlFor="smtp_host" required>
              <input
                id="smtp_host"
                value={form.host}
                onChange={(event) => update("host", event.target.value)}
                placeholder="smtp.example.com"
                required
              />
            </Field>
            <Field label="Port" htmlFor="smtp_port" required>
              <input
                id="smtp_port"
                type="number"
                min={1}
                max={65535}
                value={form.port}
                onChange={(event) => {
                  const port = event.target.value;
                  setForm((current) => ({
                    ...current,
                    port,
                    security: port === "465" ? "tls" : "starttls",
                  }));
                }}
                required
              />
            </Field>
            <Field
              label="Encryption"
              htmlFor="smtp_security"
              hint="Port 465 uses implicit TLS; 587/25/2525 use STARTTLS. Plaintext is not supported."
            >
              <select
                id="smtp_security"
                value={form.security}
                onChange={(event) => update("security", event.target.value)}
              >
                <option value="starttls">STARTTLS</option>
                <option value="tls">Implicit TLS</option>
              </select>
            </Field>
            <Field label="SMTP username" htmlFor="smtp_username" hint="Defaults to mailbox email.">
              <input
                id="smtp_username"
                value={form.username}
                onChange={(event) => update("username", event.target.value)}
                autoComplete="username"
              />
            </Field>
            <Field label="SMTP password" htmlFor="smtp_password" required>
              <input
                id="smtp_password"
                type="password"
                value={form.password}
                onChange={(event) => update("password", event.target.value)}
                autoComplete="new-password"
                required
              />
            </Field>
            <Field
              label="DKIM selectors"
              htmlFor="dkim_selectors"
              hint="Comma-separated selectors you know are configured, e.g. google, selector1."
            >
              <input
                id="dkim_selectors"
                value={form.dkim_selectors}
                onChange={(event) => update("dkim_selectors", event.target.value)}
                placeholder="selector1, selector2"
              />
            </Field>
            <div className="form-actions">
              <button className="button primary" type="submit" disabled={mutation.busy || atCapacity}>
                {mutation.busy ? "Saving…" : "Save SMTP account"}
              </button>
            </div>
          </form>
          <p className="muted small">
            The password is written to the secret store. It is never returned by the API and must
            be rotated by saving a new credential.
          </p>
        </Card>
      </div>
    </>
  );
}
