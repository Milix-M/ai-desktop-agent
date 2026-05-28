"use client";

interface Props {
  state: string;
  subtaskIndex: number;
  subtaskCount: number;
}

const STATE_CLASSES: Record<string, string> = {
  idle: "state-idle",
  understanding: "state-executing",
  planning: "state-executing",
  executing: "state-executing",
  waiting: "state-executing",
  verifying: "state-executing",
  recovering: "state-executing",
  paused: "state-paused",
  completed: "state-completed",
  failed: "state-failed",
};

export default function StatusPanel({
  state,
  subtaskIndex,
  subtaskCount,
}: Props) {
  return (
    <div className="section">
      <h2>状態</h2>
      <div className="status-row">
        状態:{" "}
        <span
          id="state-badge"
          className={`state-badge ${STATE_CLASSES[state] || "state-idle"}`}
        >
          {state.toUpperCase()}
        </span>
        {subtaskCount > 0 && (
          <span className="subtask-info">
            サブタスク {subtaskIndex + 1}/{subtaskCount}
          </span>
        )}
      </div>
    </div>
  );
}
