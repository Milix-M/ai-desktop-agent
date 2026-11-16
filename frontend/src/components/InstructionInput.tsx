"use client";

import { useRef, useState, type KeyboardEvent } from "react";

interface Props {
  onSubmit: (instruction: string, allowVmRestart: boolean) => Promise<void>;
  disabled: boolean;
}

export default function InstructionInput({ onSubmit, disabled }: Props) {
  const [value, setValue] = useState("");
  const [allowVmRestart, setAllowVmRestart] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const pendingRef = useRef(false);
  const unavailable = disabled || submitting;

  async function handleSubmit() {
    const trimmed = value.trim();
    if (!trimmed || disabled || pendingRef.current) return;
    pendingRef.current = true;
    setSubmitting(true);
    try {
      await onSubmit(trimmed, allowVmRestart);
      setValue("");
    } catch {
      // The parent shows the error; keep the instruction for retry.
    } finally {
      pendingRef.current = false;
      setSubmitting(false);
    }
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
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
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={unavailable}
      />
      <button
        id="submit-btn"
        className="submit-btn"
        onClick={handleSubmit}
        disabled={unavailable || !value.trim()}
      >
        {unavailable ? "処理中..." : "実行"}
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
