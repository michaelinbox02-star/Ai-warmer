"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { api, errorMessage } from "@/lib/api";
import { Notice } from "@/components/ui";

interface SessionInfo {
  authenticated: boolean;
}

export default function LoginPage() {
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    api<SessionInfo>("/auth/session", { redirectOnUnauthorized: false })
      .then((session) => {
        if (!cancelled && session.authenticated) router.replace("/");
      })
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) setChecking(false);
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api<SessionInfo>("/auth/login", {
        method: "POST",
        body: { password },
        redirectOnUnauthorized: false,
      });
      const params = new URLSearchParams(window.location.search);
      const next = params.get("next");
      const destination = next && next.startsWith("/") && !next.startsWith("//") ? next : "/";
      router.replace(destination);
      router.refresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-page">
      <section className="login-panel">
        <div className="login-brand">
          <span className="brand-mark large" aria-hidden="true">
            D
          </span>
          <div>
            <h1>Deliverability Console</h1>
            <p>Private transactional email control plane</p>
          </div>
        </div>

        <Notice tone="info">
          One operator identity. No public registration. Sessions are HMAC-signed and
          credentials never enter the browser.
        </Notice>

        {error && <Notice tone="danger" title="Sign-in failed">{error}</Notice>}

        <form onSubmit={submit} className="stack">
          <div className="field">
            <label htmlFor="password">Operator password</label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              autoFocus
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
              minLength={1}
              disabled={checking || busy}
            />
          </div>
          <button className="button primary full" type="submit" disabled={checking || busy}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <p className="login-footnote">
          Browser writes require the CSRF double-submit token. Repeated failed attempts are
          throttled. If this instance is not on loopback, serve it through HTTPS.
        </p>
      </section>
    </main>
  );
}
