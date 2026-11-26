"use client";

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ApiError } from "@/lib/api";
import { forgetTaskSubmission, taskSubmissionRetry } from "@/lib/task-submission-retries";
import type { PendingSubmission } from "@/lib/types";

interface Props {
  onSubmit: (instruction: string, allowVmRestart: boolean, requestId: string, workspace?: string) => Promise<void>;
  onSubmitStart?: (instruction: string) => void;
  disabled: boolean;
  initialValue?: string;
  allowWorkspace?: boolean;
  onDraftChange?: (value: string) => void;
  vmId?: string | null;
  submission?: { current: PendingSubmission | null };
}

export default function InstructionInput({ onSubmit, onSubmitStart, disabled, initialValue = "", onDraftChange, vmId = null, submission, allowWorkspace = false }: Props) {
  const localSubmission = useRef<PendingSubmission | null>(null);
  const attemptRef = submission ?? localSubmission;
  const [value, setValue] = useState(initialValue);
  const [allowVmRestart, setAllowVmRestart] = useState(() =>
    attemptRef.current?.instruction === initialValue.trim() ? attemptRef.current.allowVmRestart : false);
  const [folderChoice, setFolderChoice] = useState({ vmId: attemptRef.current?.vmId ?? vmId, path: attemptRef.current?.workspace ?? "" });
  const folder = folderChoice.vmId === vmId ? folderChoice.path : "";
  const setFolder = (path: string) => setFolderChoice({ vmId, path });
  const workspace = allowWorkspace ? folder.trim() || undefined : undefined;
  const [submitting, setSubmitting] = useState(false);
  const pendingRef = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const unavailable = disabled || submitting;
  const sameAttempt = attemptRef.current?.instruction === value.trim() &&
    attemptRef.current?.vmId === vmId && attemptRef.current?.allowVmRestart === allowVmRestart &&
    attemptRef.current?.workspace === workspace;
  const retryAsNew = sameAttempt && attemptRef.current?.gone;

  async function handleSubmit() {
    const trimmed = value.trim();
    if (!trimmed || disabled || pendingRef.current) return;
    if (!sameAttempt || retryAsNew) {
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      attemptRef.current = {
        instruction: trimmed, vmId, allowVmRestart, workspace,
        requestId: Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join(""),
      };
    }
    let attempt = attemptRef.current!;
    pendingRef.current = true;
    setSubmitting(true);
    try {
      onSubmitStart?.(trimmed);
      const requestId = await taskSubmissionRetry(attempt, Boolean(retryAsNew));
      if (!mounted.current) return;
      if (attemptRef.current === attempt) {
        attempt = { ...attempt, requestId };
        attemptRef.current = attempt;
      } else {
        // A different mounted input has taken ownership while hashing this submission.
        return;
      }
      if (workspace) await onSubmit(trimmed, allowVmRestart, attempt.requestId, workspace);
      else await onSubmit(trimmed, allowVmRestart, attempt.requestId);
      forgetTaskSubmission(attempt.requestId);
      if (mounted.current && attemptRef.current?.requestId === attempt.requestId) {
        attemptRef.current = null;
        setValue("");
        setFolder("");
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
      {allowWorkspace && <details>
        <summary>ファイルの一括操作</summary>
        <label>Documents 内の対象フォルダー
          <input value={folder} onChange={(event) => setFolder(event.target.value)}
            maxLength={500} disabled={unavailable} placeholder="例: Projects/April" />
        </label>
      </details>}
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
