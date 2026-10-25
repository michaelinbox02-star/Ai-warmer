"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { api, errorMessage } from "@/lib/api";
import { Badge, Loading, Notice } from "@/components/ui";

interface SessionInfo {
  authenticated: boolean;
  actor: string | null;
  method: string | null;
  csrf_token: string | null;
  session_expires_in: number | null;
}

const NAV = [
  { href: "/", label: "Dashboard", exact: true },
  { href: "/accounts", label: "Accounts" },
  { href: "/messages", label: "Messages" },
  { href: "/tests", label: "Controlled tests" },
  { href: "/reports", label: "Reports" },
  { href: "/suppressions", label: "Suppressions" },
  { href: "/settings", label: "Settings" },
];

function navActive(pathname: string, href: string, exact?: boolean) {
  if (exact) return pathname === href;
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function ConsoleChrome({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [error, setError] = useState("");
  const [signingOut, setSigningOut] = useState(false);

  const load = useCallback(async () => {
    setError("");
    try {
      const result = await api<SessionInfo>("/auth/session", {
        redirectOnUnauthorized: false,
      });
      if (!result.authenticated) {
        router.replace(`/login?next=${encodeURIComponent(window.location.pathname)}`);
        return;
      }
      setSession(result);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [router]);

  useEffect(() => {
    void load();
  }, [load]);

  async function signOut() {
    setSigningOut(true);
    try {
      await api<SessionInfo>("/auth/logout", { method: "POST" });
    } catch {
      // Cookies may already be invalid; navigating to login is still correct.
    } finally {
      router.replace("/login");
      router.refresh();
      setSigningOut(false);
    }
  }

  if (error) {
    return (
      <main className="centered-page">
        <Notice tone="danger" title="Cannot reach the operator API">
          {error}
        </Notice>
        <button className="button secondary" onClick={() => void load()} type="button">
          Retry
        </button>
      </main>
    );
  }

  if (!session) {
    return (
      <main className="centered-page">
        <Loading label="Checking operator session…" />
      </main>
    );
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Link href="/" className="brand">
          <span className="brand-mark" aria-hidden="true">
            D
          </span>
          <span>
            <strong>Deliverability</strong>
            <small>Private operator console</small>
          </span>
        </Link>

        <nav className="nav" aria-label="Main navigation">
          {NAV.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={navActive(pathname, item.href, item.exact) ? "active" : ""}
            >
              {item.label}
            </Link>
          ))}
        </nav>

        <div className="sidebar-footer">
          <div className="operator-line">
            <span className="operator-dot" aria-hidden="true" />
            <span>
              <strong>{session.actor || "operator"}</strong>
              <small>{session.method === "token" ? "automation token" : "session"}</small>
            </span>
          </div>
          <button
            type="button"
            className="button ghost full"
            onClick={() => void signOut()}
            disabled={signingOut}
          >
            {signingOut ? "Signing out…" : "Sign out"}
          </button>
          <p className="sidebar-note">
            Credentials live in the secret store, never in PostgreSQL or this browser.
          </p>
        </div>
      </aside>

      <div className="content-column">
        <header className="mobile-header">
          <Link href="/" className="brand compact">
            <span className="brand-mark" aria-hidden="true">
              D
            </span>
            <strong>Deliverability</strong>
          </Link>
          <Badge tone="info">private</Badge>
        </header>
        <main className="main-content">{children}</main>
        <footer className="app-footer">
          Acceptance is not delivery. Inbox placement requires independent evidence.
        </footer>
      </div>
    </div>
  );
}
