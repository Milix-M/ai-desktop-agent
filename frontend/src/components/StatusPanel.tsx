"use client";

import type { SubtaskInfo } from "@/lib/types";

interface Props {
  state: string;
  subtaskIndex: number;
  subtaskCount: number;
  subtasks?: SubtaskInfo[];
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
  interrupted: "state-failed",
};

export default function StatusPanel({
  state,
  subtaskIndex,
  subtaskCount,
  subtasks = [],
}: Props) {
  const doneCount =
    state === "completed"
      ? subtasks.length
      : Math.min(subtaskIndex, subtasks.length);
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
            サブタスク {Math.min(subtaskIndex + 1, subtaskCount)}/{subtaskCount}
          </span>
        )}
      </div>
      {subtasks.length > 0 && (
        <ul className="subtask-list" data-testid="subtask-list">
          {subtasks.map((st, i) => {
            const done = i < doneCount;
            const current = i === doneCount && state !== "completed";
            return (
              <li
                key={st.id}
                className={`subtask-item${done ? " done" : ""}${current ? " current" : ""}`}
              >
                <span className="subtask-mark">{done ? "✓" : current ? "▶" : "○"}</span>
                <span className="subtask-desc">{st.description}</span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
