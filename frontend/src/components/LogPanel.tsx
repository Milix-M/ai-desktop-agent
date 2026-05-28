"use client";

import { useEffect, useRef } from "react";
import type { LogEntry } from "@/lib/types";

interface Props {
  entries: LogEntry[];
}

export default function LogPanel({ entries }: Props) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ behavior: "smooth" });
  }, [entries.length]);

  function escapeHtml(str: string): string {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  return (
    <div className="section log-section">
      <h2>📜 ログ</h2>
      <div className="log">
        {entries.length === 0 && (
          <div className="entry state">
            <span className="time">--:--</span>
            待機中...
          </div>
        )}
        {entries.map((entry) => (
          <div key={entry.id} className={`entry ${entry.level}`}>
            <span className="time">{entry.time}</span>
            <span
              dangerouslySetInnerHTML={{
                __html: escapeHtml(entry.message),
              }}
            />
          </div>
        ))}
        <div ref={endRef} />
      </div>
    </div>
  );
}
