"use client";

import type { TaskHistoryItem } from "@/lib/types";

interface Props {
  items: TaskHistoryItem[];
  selectedId: string | null;
  onSelect: (taskId: string) => void;
}

const STATE_LABEL: Record<string, string> = {
  completed: "完了",
  failed: "失敗",
  interrupted: "中断",
  executing: "実行中",
  paused: "一時停止",
};

function timeStr(epochSec: number): string {
  if (!epochSec) return "--:--";
  return new Date(epochSec * 1000).toLocaleString("ja-JP", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function TaskHistory({ items, selectedId, onSelect }: Props) {
  if (items.length === 0) return null;

  return (
    <div className="section">
      <h2>履歴</h2>
      <div className="history-list">
        {items.map((item) => (
          <button
            key={item.id}
            className={`history-item${item.id === selectedId ? " selected" : ""}`}
            onClick={() => onSelect(item.id)}
          >
            <span className="history-state">
              {STATE_LABEL[item.state] ?? item.state}
            </span>
            <span className="history-instruction">
              {item.instruction || "(指示なし)"}
            </span>
            <span className="history-meta">
              {item.action_count}操作・{timeStr(item.updated_at)}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
