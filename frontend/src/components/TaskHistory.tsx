"use client";

import { useEffect, useRef, useState } from "react";
import type { TaskHistoryItem } from "@/lib/types";

interface Props {
  items: TaskHistoryItem[];
  selectedId: string | null;
  onSelect: (taskId: string) => void;
  onDelete: (taskId: string) => void | Promise<void>;
  detailError?: { taskId: string; message: string } | null;
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

export default function TaskHistory({ items, selectedId, onSelect, onDelete, detailError }: Props) {
  const [deleting, setDeleting] = useState<string | null>(null);
  const [error, setError] = useState<{ taskId: string; message: string } | null>(null);
  const pending = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  async function remove(taskId: string) {
    if (pending.current) return;
    pending.current = true;
    setDeleting(taskId);
    setError(null);
    try {
      await onDelete(taskId);
    } catch (cause) {
      if (mounted.current) setError({ taskId,
        message: `削除を確認できません: ${cause instanceof Error ? cause.message : String(cause)}` });
    } finally {
      pending.current = false;
      if (mounted.current) setDeleting(null);
    }
  }

  return (
    <div className="section">
      <h2>履歴（{items.length}件）</h2>
      {items.length === 0 ? (
        <div className="history-empty">履歴なし</div>
      ) : (
      <div className="history-list">
        {items.map((item) => (
          <div key={item.id}>
            <div className={`history-item${item.id === selectedId ? " selected" : ""}`}>
              <button className="history-main" onClick={() => onSelect(item.id)} title={item.instruction || undefined}>
                <span className="history-row">
                  <span className="history-instruction">
                    {item.instruction || "(指示なし)"}
                  </span>
                </span>
                <span className="history-row">
                  <span className="history-meta">
                    {timeStr(item.updated_at)}
                    {(item.decision_calls ?? 0) > 0 &&
                      ` 決定${item.decision_calls}件・退避${item.decision_escalations ?? 0}件`}
                  </span>
                </span>
              </button>
              <button
                className="history-delete"
                aria-label={`${item.instruction || item.id}を削除`}
                disabled={deleting !== null}
                aria-busy={deleting === item.id}
                onClick={() => { void remove(item.id); }}
              >
                ×
              </button>
            </div>
            {error?.taskId === item.id && <div role="alert" className="history-error">{error.message}</div>}
            {detailError?.taskId === item.id && <div role="alert" className="history-error">{detailError.message}</div>}
          </div>
        ))}
      </div>
      )}
    </div>
  );
}
