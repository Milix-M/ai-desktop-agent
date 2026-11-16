"use client";

import type { ManualInputRequest } from "@/lib/types";

interface Props {
  onControl: (action: "pause" | "resume" | "stop") => Promise<void>;
  state: string;
  manualInput?: ManualInputRequest | null;
}

const RUNNING_STATES = [
  "understanding",
  "planning",
  "executing",
  "waiting",
  "verifying",
  "recovering",
];

export default function ControlPanel({ onControl, state, manualInput }: Props) {
  const isRunning = RUNNING_STATES.includes(state);
  const awaitingInput = state === "awaiting_user";

  return (
    <div className="section">
      {awaitingInput && manualInput && <div role="status" className="manual-input">
        <p>{manualInput.reason}</p>
        <p>デスクトップで入力し、秘密情報を画面から隠してから再開してください。</p>
      </div>}
      <div className="controls">
        {!awaitingInput && <button
          disabled={state !== "executing"}
          onClick={() => onControl("pause")}
        >
          一時停止
        </button>}
        <button
          className={awaitingInput ? "manual-resume" : undefined}
          disabled={awaitingInput ? !manualInput?.id : state !== "paused"}
          onClick={() => onControl("resume")}
        >
          {awaitingInput ? "画面を送信して再開" : "再開"}
        </button>
        <button
          disabled={!isRunning && state !== "paused" && !awaitingInput}
          onClick={() => onControl("stop")}
        >
          停止
        </button>
      </div>
    </div>
  );
}
