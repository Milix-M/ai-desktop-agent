"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { addSchedule, deleteSchedule, getSchedules } from "@/lib/api";
import type { ScheduleEnvironmentKind, TaskSchedule } from "@/lib/types";

const intervals = new Map([[0, "1回のみ"], [86400, "24時間ごと"], [604800, "7日ごと"]]);

const disposableTargets = { container: "コンテナ", qemu: "QEMU VM", android: "Android" };
type Target = "existing" | ScheduleEnvironmentKind;

interface Props {
  vmId: string | null;
  onSelectTask: (taskId: string) => void;
}

export default function SchedulePanel({ vmId, onSelectTask }: Props) {
  const attempt = useRef<{ payload: string; id: string } | null>(null);
  const [instruction, setInstruction] = useState("");
  const [target, setTarget] = useState<Target>(vmId ? "existing" : "container");
  const targetId = target === "existing" ? vmId : null;
  const targetKind = target === "existing" ? null : target;
  const [when, setWhen] = useState("");
  const [interval, setIntervalValue] = useState(0);
  const [rows, setRows] = useState<TaskSchedule[]>([]);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const schedules = await getSchedules(controller.signal);
        if (!controller.signal.aborted) {
          setRows(schedules);
          setLoadError("");
        }
      } catch (e) {
        if (!controller.signal.aborted) setLoadError(String(e));
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(refresh, 5000);
      }
    }
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [revision]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy || (!targetId && !targetKind)) return;
    const runAt = new Date(when).getTime();
    if (!Number.isFinite(runAt) || runAt <= Date.now()) {
      setError("未来の日時を指定してください");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const payload = JSON.stringify([targetId, targetKind, instruction.trim(), runAt, interval]);
      if (attempt.current?.payload !== payload) {
        const bytes = crypto.getRandomValues(new Uint8Array(16));
        attempt.current = { payload, id: Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("") };
      }
      await addSchedule(targetId, instruction.trim(), Math.floor(runAt / 1000), interval, attempt.current.id, targetKind);
      attempt.current = null;
      setInstruction("");
      setWhen("");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
      setRevision(n => n + 1);
    }
  }

  async function remove(id: string) {
    setBusy(true);
    setError("");
    try {
      await deleteSchedule(id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
      setRevision(n => n + 1);
    }
  }

  return (
    <div className="schedule-panel">
      <form onSubmit={save}>
        <label>実行環境<select value={target} disabled={busy} onChange={e => setTarget(e.target.value as Target)}>
          {vmId && <option value="existing">選択中の環境（{vmId}）</option>}
          <optgroup label="実行ごとに作成し、終了後に削除">
            {Object.entries(disposableTargets).map(([kind, label]) =>
              <option key={kind} value={kind}>{label}（使い捨て）</option>)}
          </optgroup>
        </select></label>
        <label>指示<textarea className="instruction-input" value={instruction} required maxLength={20000}
          onChange={e => setInstruction(e.target.value)} disabled={busy} /></label>
        <label>開始日時（{Intl.DateTimeFormat().resolvedOptions().timeZone}）
          <input type="datetime-local" value={when} required disabled={busy} onChange={e => setWhen(e.target.value)} />
        </label>
        <label>繰り返し<select value={interval} disabled={busy} onChange={e => setIntervalValue(Number(e.target.value))}>
          {Array.from(intervals, ([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
        <button className="submit-btn" disabled={busy || (!targetId && !targetKind) || !when || !instruction.trim()}>予約する</button>
      </form>
      {error && <p role="alert">{error}</p>}
      {loadError && <p role="alert">{loadError}</p>}
      {rows.map(row => (
        <div className="schedule-item" key={row.id}>
          <strong>{row.instruction}</strong>
          <span>{row.vm_kind ? `${disposableTargets[row.vm_kind]}（使い捨て）` : row.vm_id} · {intervals.get(row.interval)}</span>
          <span>{row.preparing ? "環境を準備中" : row.next_run === null ? "予約日時" : "次回"} {new Date((row.next_run ?? row.run_at) * 1000).toLocaleString()}</span>
          {row.last_error && <span>開始失敗 · {row.last_error}</span>}
          <div className="controls">
            {row.last_task_id && <button onClick={() => onSelectTask(row.last_task_id!)}>前回のタスクを見る</button>}
            <button disabled={busy} onClick={() => remove(row.id)}>予約を削除</button>
          </div>
        </div>
      ))}
    </div>
  );
}
