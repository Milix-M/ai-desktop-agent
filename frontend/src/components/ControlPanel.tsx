"use client";

interface Props {
  onControl: (action: "pause" | "resume" | "stop") => Promise<void>;
  state: string;
}

const RUNNING_STATES = [
  "understanding",
  "planning",
  "executing",
  "waiting",
  "verifying",
  "recovering",
];

export default function ControlPanel({ onControl, state }: Props) {
  const isRunning = RUNNING_STATES.includes(state);
  const disabled = !isRunning && state !== "paused";

  return (
    <div className="section">
      <h2>🎮 操作</h2>
      <div className="controls">
        <button
          disabled={state !== "executing"}
          onClick={() => onControl("pause")}
        >
          ⏸ 一時停止
        </button>
        <button
          disabled={state !== "paused"}
          onClick={() => onControl("resume")}
        >
          ▶ 再開
        </button>
        <button
          disabled={!isRunning && state !== "paused"}
          onClick={() => onControl("stop")}
        >
          ⏹ 停止
        </button>
      </div>
    </div>
  );
}
