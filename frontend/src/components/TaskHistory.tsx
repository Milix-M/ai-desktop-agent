"use client";

import type { TaskHistoryItem } from "@/lib/types";

interface Props {
  items: TaskHistoryItem[];
  selectedId: string | null;
  onSelect: (taskId: string) => void;
  onDelete: (taskId: string) => void;
  vmNames?: Record<string, string>;
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

export default function TaskHistory({ items, selectedId, onSelect, onDelete, vmNames = {} }: Props) {
  if (items.length === 0) return null;

  return (
    <div className="section">
      <h2>履歴</h2>
      <div className="history-list">
        {items.map((item) => (
          <div
            key={item.id}
            className={`history-item${item.id === selectedId ? " selected" : ""}`}
          >
            <button className="history-main" onClick={() => onSelect(item.id)} title={item.instruction || undefined}>
              <span className="history-row">
                <span className="history-state">
                  {STATE_LABEL[item.state] ?? item.state}
                </span>
                <span className="history-instruction">
                  {item.instruction || "(指示なし)"}
                </span>
              </span>
              <span className="history-row">
                <span className="history-meta">
                  {item.vm_id && vmNames[item.vm_id] ? `[${vmNames[item.vm_id]}] ` : ""}
                  {item.action_count}操作・成功{item.success_count}・失敗{item.failure_count}・{timeStr(item.updated_at)}
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
    </div>
  );
}
