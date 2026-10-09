"use client";

import type { TaskHistoryItem } from "@/lib/types";

interface Props {
  items: TaskHistoryItem[];
  selectedId: string | null;
  onSelect: (taskId: string) => void;
  onDelete: (taskId: string) => void;
}

function timeStr(epochSec: number): string {
  if (!epochSec) return "--:--";
  return new Date(epochSec * 1000).toLocaleString("ja-JP", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function TaskHistory({ items, selectedId, onSelect, onDelete }: Props) {
  return (
    <div className="section">
      <h2>履歴（{items.length}件）</h2>
      {items.length === 0 ? (
        <div className="history-empty">履歴なし</div>
      ) : (
      <div className="history-list">
        {items.map((item) => (
          <div
            key={item.id}
            className={`history-item${item.id === selectedId ? " selected" : ""}`}
          >
            <button className="history-main" onClick={() => onSelect(item.id)} title={item.instruction || undefined}>
              <span className="history-row">
                <span className="history-instruction">
                  {item.instruction || "(指示なし)"}
                </span>
              </span>
              <span className="history-row">
                <span className="history-meta">
                  {timeStr(item.updated_at)}
                </span>
              </span>
            </button>
            <button
              className="history-delete"
              aria-label={`${item.instruction || item.id}を削除`}
              onClick={() => onDelete(item.id)}
            >
              ×
            </button>
          </div>
        ))}
      </div>
      )}
    </div>
  );
}
