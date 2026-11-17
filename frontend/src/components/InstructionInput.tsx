"use client";

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ApiError } from "@/lib/api";
import type { PendingSubmission } from "@/lib/types";

interface Props {
  onSubmit: (instruction: string, allowVmRestart: boolean, requestId: string) => Promise<void>;
  disabled: boolean;
  initialValue?: string;
  onDraftChange?: (value: string) => void;
  vmId?: string | null;
  submission?: { current: PendingSubmission | null };
}

export default function InstructionInput({ onSubmit, disabled, initialValue = "", onDraftChange, vmId = null, submission }: Props) {
  const localSubmission = useRef<PendingSubmission | null>(null);
  const attemptRef = submission ?? localSubmission;
  const [value, setValue] = useState(initialValue);
  const [allowVmRestart, setAllowVmRestart] = useState(() =>
    attemptRef.current?.instruction === initialValue.trim() ? attemptRef.current.allowVmRestart : false);
  const [submitting, setSubmitting] = useState(false);
  const pendingRef = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const unavailable = disabled || submitting;
  const sameAttempt = attemptRef.current?.instruction === value.trim() &&
    attemptRef.current?.vmId === vmId && attemptRef.current?.allowVmRestart === allowVmRestart;
  const retryAsNew = sameAttempt && attemptRef.current?.gone;

  async function handleSubmit() {
    const trimmed = value.trim();
    if (!trimmed || disabled || pendingRef.current) return;
    if (!sameAttempt || retryAsNew) {
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      attemptRef.current = {
        instruction: trimmed, vmId, allowVmRestart,
        requestId: Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join(""),
      };
    }
    const attempt = attemptRef.current!;
    pendingRef.current = true;
    setSubmitting(true);
    try {
      await onSubmit(trimmed, allowVmRestart, attempt.requestId);
      if (mounted.current && attemptRef.current?.requestId === attempt.requestId) {
        attemptRef.current = null;
        setValue("");
        onDraftChange?.("");
      }
    } catch (error) {
      // The parent shows the error; keep the instruction for retry.
      if (mounted.current && attemptRef.current?.requestId === attempt.requestId &&
          error instanceof ApiError && error.status === 410) {
        attemptRef.current = { ...attempt, gone: true };
      }
    } finally {
      pendingRef.current = false;
      if (mounted.current) setSubmitting(false);
    }
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!retryAsNew) handleSubmit();
    }
  }

  return (
    <div className="section">
      <h2>指示</h2>
      <textarea
        id="instruction"
        aria-label="指示"
        maxLength={20000}
        className="instruction-input"
        placeholder="例: LibreOfficeで表を作成して /data/report.ods に保存してください"
        value={value}
        onChange={(e) => { setValue(e.target.value); onDraftChange?.(e.target.value); }}
        onKeyDown={handleKeyDown}
        disabled={unavailable}
      />
      <button
        id="submit-btn"
        className="submit-btn"
        onClick={handleSubmit}
        disabled={unavailable || !value.trim()}
      >
        {submitting ? "処理中..." : retryAsNew ? "新しいタスクとして実行" : sameAttempt ? "再送" : "実行"}
      </button>
      <label className="allow-restart">
        <input
          type="checkbox"
          checked={allowVmRestart}
          onChange={(e) => setAllowVmRestart(e.target.checked)}
          disabled={unavailable}
        />
        VM再起動を許可する
      </label>
    </div>
  );
}
