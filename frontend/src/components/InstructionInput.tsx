"use client";

import { useState, type KeyboardEvent } from "react";

interface Props {
  onSubmit: (instruction: string) => Promise<void>;
  disabled: boolean;
}

export default function InstructionInput({ onSubmit, disabled }: Props) {
  const [value, setValue] = useState("");

  async function handleSubmit() {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    setValue("");
    try {
      await onSubmit(trimmed);
    } catch {
      // handled by parent
    }
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  }

  return (
    <div className="section">
      <h2>📝 指示</h2>
      <textarea
        id="instruction"
        className="instruction-input"
        placeholder="例: LibreOfficeで表を作成して /data/report.ods に保存してください"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={disabled}
      />
      <button
        id="submit-btn"
        className="submit-btn"
        onClick={handleSubmit}
        disabled={disabled || !value.trim()}
      >
        {disabled ? "⏳ 処理中..." : "▶ 実行"}
      </button>
    </div>
  );
}
