"use client";

import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { getAuthStatus, login, logout } from "@/lib/api";

export default function AuthGate({ children, onLogout }: {
  children: (actions: ReactNode) => ReactNode;
  onLogout?: () => void;
}) {
  const [state, setState] = useState<"checking" | "locked" | "ready">("checking");
  const [key, setKey] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);

  useEffect(() => {
    let disposed = false;
    getAuthStatus().then(({ authenticated }) => {
      if (!disposed) setState(authenticated ? "ready" : "locked");
    }).catch(() => {
      if (!disposed) { setState("locked"); setError("サーバーに接続できません"); }
    });
    const lock = () => { setState("locked"); setKey(""); setError("ログインしてください"); };
    window.addEventListener("auth-required", lock);
    return () => { disposed = true; window.removeEventListener("auth-required", lock); };
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current || !key) return;
    submitting.current = true;
    setBusy(true); setError("");
    try {
      await login(key);
      setKey(""); setState("ready");
    } catch (error) {
      setError(error instanceof Error ? error.message : "ログインできません");
    } finally {
      submitting.current = false; setBusy(false);
    }
  }

  async function signOut() {
    if (submitting.current) return;
    submitting.current = true; setBusy(true);
    try {
      await logout();
      onLogout?.();
      setState("locked"); setKey(""); setError("");
    } catch {
      setError("ログアウトできません。接続を確認してください");
    } finally {
      submitting.current = false; setBusy(false);
    }
  }

  if (state === "ready") return children(
    <div className="session-actions">
      {error && <span role="alert">{error}</span>}
      <button type="button" onClick={signOut} disabled={busy}>ログアウト</button>
    </div>
  );

  return <main className="login-screen">
    <form className="login-form" onSubmit={submit}>
      <h1>AI Desktop Agent</h1>
      {state === "checking" ? <p role="status">接続中…</p> : <>
        <label htmlFor="access-key">接続キー</label>
        <input id="access-key" type="password" autoComplete="off" value={key}
          onChange={(event) => setKey(event.target.value)} maxLength={256} required disabled={busy} />
        {error && <p role="alert">{error}</p>}
        <button type="submit" disabled={busy || !key}>{busy ? "接続中…" : "ログイン"}</button>
      </>}
    </form>
  </main>;
}
