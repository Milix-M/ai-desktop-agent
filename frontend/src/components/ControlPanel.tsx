"use client";

import type { ManualInputRequest } from "@/lib/types";

interface Props {
  onControl: (action: "pause" | "resume" | "stop") => Promise<void>;
  state: string;
  manualInput?: ManualInputRequest | null;
  manualInputReady?: boolean;
  disabled?: boolean;
}

const RUNNING_STATES = [
  "understanding",
  "planning",
  "executing",
  "waiting",
  "verifying",
  "recovering",
];

export default function ControlPanel({ onControl, state, manualInput, manualInputReady = true, disabled = false }: Props) {
  const isRunning = RUNNING_STATES.includes(state);
  const awaitingInput = state === "awaiting_user";

  return (
    <div className="section">
      {awaitingInput && manualInput && <div role="status" className="manual-input">
        <p>{manualInput.reason}</p>
        {manualInputReady && <p>デスクトップで入力し、秘密情報を画面から隠してから再開してください。</p>}
      </div>}
      <div className="controls">
        {!awaitingInput && <button
          disabled={disabled || state !== "executing"}
          onClick={() => onControl("pause")}
        >
          一時停止
        </button>}
        <button
          className={awaitingInput ? "manual-resume" : undefined}
          disabled={disabled || (awaitingInput ? !manualInput?.id || !manualInputReady : state !== "paused")}
          onClick={() => onControl("resume")}
        >
          {awaitingInput ? "画面を送信して再開" : "再開"}
        </button>
        <button
          disabled={disabled || (!isRunning && state !== "paused" && !awaitingInput)}
          onClick={() => onControl("stop")}
        >
          停止
        </button>
      </div>
    </div>
  );
}
