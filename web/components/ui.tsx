import type { ReactNode } from "react";

export function Badge({
  children,
  tone = "neutral",
  title,
}: {
  children: ReactNode;
  tone?: "neutral" | "success" | "warning" | "danger" | "info" | "muted";
  title?: string;
}) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  );
}

const STATUS_TONES: Record<string, "neutral" | "success" | "warning" | "danger" | "info" | "muted"> = {
  connected: "success",
  pass: "success",
  auth_pass: "success",
  completed: "success",
  delivered: "success",
  submitted: "info",
  queued: "neutral",
  running: "info",
  sending: "info",
  retry_wait: "warning",
  pending: "neutral",
  warning: "warning",
  needs_review: "warning",
  completed_with_errors: "warning",
  auth_required: "danger",
  error: "danger",
  failed: "danger",
  bounced: "danger",
  complained: "danger",
  cancelled: "muted",
  expired: "muted",
  disabled: "muted",
  test_only: "info",
  production: "success",
  paused: "warning",
  open: "danger",
  half_open: "warning",
  closed: "success",
  timeout: "danger",
  timed_out: "warning",
  unknown: "warning",
  not_applicable: "muted",
  observed_received: "info",
};

export function StatusBadge({ status, label }: { status: string; label?: string }) {
  return (
    <Badge tone={STATUS_TONES[status] || "neutral"}>
      {label || status.replaceAll("_", " ")}
    </Badge>
  );
}

export function Card({
  children,
  className = "",
  title,
  subtitle,
  actions,
}: {
  children: ReactNode;
  className?: string;
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card-header">
          <div>
            {title && <h2 className="card-title">{title}</h2>}
            {subtitle && <p className="card-subtitle">{subtitle}</p>}
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

export function EmptyState({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function Notice({
  tone = "info",
  title,
  children,
}: {
  tone?: "info" | "success" | "warning" | "danger";
  title?: string;
  children?: ReactNode;
}) {
  return (
    <div className={`notice notice-${tone}`} role={tone === "danger" ? "alert" : "status"}>
      {title && <strong>{title}</strong>}
      {children && <div>{children}</div>}
    </div>
  );
}

export function Field({
  label,
  hint,
  htmlFor,
  required,
  children,
}: {
  label: string;
  hint?: ReactNode;
  htmlFor?: string;
  required?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="field">
      <label htmlFor={htmlFor}>
        {label}
        {required && <span className="required" aria-hidden="true"> *</span>}
      </label>
      {children}
      {hint && <small>{hint}</small>}
    </div>
  );
}

export function KeyValue({
  items,
  columns = 2,
}: {
  items: Array<{ label: string; value: ReactNode }>;
  columns?: 1 | 2 | 3;
}) {
  return (
    <dl className={`key-values columns-${columns}`}>
      {items.map((item) => (
        <div key={item.label}>
          <dt>{item.label}</dt>
          <dd>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Meter({
  used,
  limit,
  label,
}: {
  used: number;
  limit: number;
  label: string;
}) {
  const percent = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  const tone = percent >= 90 ? "danger" : percent >= 70 ? "warning" : "ok";
  return (
    <div className="meter">
      <div className="meter-label">
        <span>{label}</span>
        <span>
          {used} / {limit}
        </span>
      </div>
      <div className="meter-track">
        <div className={`meter-fill meter-${tone}`} style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}

export function Code({ children, block = false }: { children: ReactNode; block?: boolean }) {
  return block ? <pre className="code-block">{children}</pre> : <code>{children}</code>;
}
