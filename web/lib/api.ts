export class ApiError extends Error {
  status: number;
  code: string;
  details: Record<string, unknown>;

  constructor(
    message: string,
    options: { status: number; code?: string; details?: Record<string, unknown> },
  ) {
    super(message);
    this.name = "ApiError";
    this.status = options.status;
    this.code = options.code || "request_failed";
    this.details = options.details || {};
  }
}

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const prefix = `${name}=`;
  const match = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix));
  return match ? decodeURIComponent(match.slice(prefix.length)) : null;
}

export async function api<T>(
  path: string,
  options: {
    method?: "GET" | "POST" | "PATCH" | "DELETE";
    body?: unknown;
    headers?: Record<string, string>;
    redirectOnUnauthorized?: boolean;
  } = {},
): Promise<T> {
  const method = options.method || "GET";
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(options.headers || {}),
  };

  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (method !== "GET") {
    const csrf = readCookie("dlp_csrf");
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }

  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      method,
      headers,
      credentials: "include",
      cache: "no-store",
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
  } catch {
    throw new ApiError("The API is unreachable. Check the backend and network.", {
      status: 0,
      code: "api_unreachable",
    });
  }

  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json().catch(() => null)
    : null;

  if (!response.ok) {
    const error = payload?.error || {};
    if (response.status === 401 && options.redirectOnUnauthorized !== false) {
      if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
        window.location.assign("/login");
      }
    }
    throw new ApiError(error.message || `Request failed (${response.status})`, {
      status: response.status,
      code: error.code,
      details: error.details,
    });
  }

  if (response.status === 204) return undefined as T;
  return (payload ?? ({} as T)) as T;
}

export function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") {
      search.set(key, String(value));
    }
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong";
}

export function errorDetails(error: unknown): string[] {
  if (!(error instanceof ApiError)) return [];
  const issues = error.details?.issues;
  if (!Array.isArray(issues)) return [];
  return issues.map((issue) => {
    if (!issue || typeof issue !== "object") return String(issue);
    const item = issue as { location?: unknown; message?: unknown };
    const location = Array.isArray(item.location) ? item.location.join(".") : "";
    return location ? `${location}: ${String(item.message)}` : String(item.message);
  });
}
