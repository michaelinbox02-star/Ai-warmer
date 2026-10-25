"use client";

import { useCallback, useEffect, useState } from "react";

import { api, errorDetails, errorMessage } from "@/lib/api";

export function useApiData<T>(path: string | null) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(Boolean(path));
  const [version, setVersion] = useState(0);

  const reload = useCallback(() => setVersion((value) => value + 1), []);

  useEffect(() => {
    if (!path) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError("");
    api<T>(path)
      .then((result) => {
        if (!cancelled) setData(result);
      })
      .catch((err) => {
        if (!cancelled) setError(errorMessage(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [path, version]);

  return { data, error, loading, reload, setData };
}

export function useMutation() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [issues, setIssues] = useState<string[]>([]);
  const [message, setMessage] = useState("");

  const run = useCallback(
    async <T,>(action: () => Promise<T>, successMessage = ""): Promise<T | null> => {
      setBusy(true);
      setError("");
      setIssues([]);
      setMessage("");
      try {
        const result = await action();
        if (successMessage) setMessage(successMessage);
        return result;
      } catch (err) {
        setError(errorMessage(err));
        setIssues(errorDetails(err));
        return null;
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  function reset() {
    setError("");
    setIssues([]);
    setMessage("");
  }

  return { busy, error, issues, message, run, reset };
}
