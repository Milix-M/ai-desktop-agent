"use client";

import { useState, useCallback } from "react";
import VncViewer from "@/components/VncViewer";
import InstructionInput from "@/components/InstructionInput";
import StatusPanel from "@/components/StatusPanel";
import ControlPanel from "@/components/ControlPanel";
import LogPanel from "@/components/LogPanel";
import StatusBar from "@/components/StatusBar";
import { useWebSocket } from "@/hooks/useWebSocket";
import { createTask, controlTask } from "@/lib/api";
import type { WsMessage, LogEntry } from "@/lib/types";

let logIdCounter = 0;

function timeStr(): string {
  return new Date().toLocaleTimeString("ja-JP", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function Home() {
  const [state, setState] = useState("idle");
  const [subtaskIndex, setSubtaskIndex] = useState(0);
  const [subtaskCount, setSubtaskCount] = useState(0);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [vncConnected, setVncConnected] = useState(false);
  const [vmResolution, setVmResolution] = useState<string | undefined>();

  const addLog = useCallback(
    (message: string, level: LogEntry["level"]) => {
      const entry: LogEntry = {
        id: logIdCounter++,
        time: timeStr(),
        message,
        level,
      };
      setLogs((prev) => [...prev, entry]);
    },
    []
  );

  const handleWsMessage = useCallback(
    (data: WsMessage) => {
      switch (data.type) {
        case "state":
          setState(data.state);
          setSubtaskIndex(data.subtask_index);
          setSubtaskCount(data.subtask_count);
          addLog(data.state, "state");
          break;

        case "action":
          addLog(
            `${data.action_type} ${data.description || ""}`,
            data.success ? "action" : "error"
          );
          break;

        case "error":
          addLog(data.message, "error");
          break;

        case "complete":
          addLog(
            data.success ? "タスク完了" : "タスク失敗",
            data.success ? "complete" : "error"
          );
          setState(data.success ? "completed" : "failed");
          break;
      }
    },
    [addLog]
  );

  useWebSocket(handleWsMessage);

  const handleVncChange = useCallback(
    (connected: boolean, resolution?: string) => {
      setVncConnected(connected);
      if (resolution) setVmResolution(resolution);
    },
    []
  );

  const RUNNING_STATES = [
    "understanding",
    "planning",
    "executing",
    "waiting",
    "verifying",
    "recovering",
  ];
  const isRunning = RUNNING_STATES.includes(state);
  const submitDisabled = isRunning || state === "paused";

  const handleSubmit = useCallback(
    async (instruction: string) => {
      addLog(`${instruction}`, "action");
      const result = await createTask(instruction);
      setState(result.state);
    },
    [addLog]
  );

  const handleControl = useCallback(
    async (action: "pause" | "resume" | "stop") => {
      try {
        await controlTask(action);
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        addLog(`操作エラー: ${msg}`, "error");
      }
    },
    [addLog]
  );

  return (
    <div className="app-container">
      <div className="main-layout">
        <VncViewer onConnectionChange={handleVncChange} />

        <div className="sidebar">
          <h1>AI Desktop Agent</h1>

          <InstructionInput
            onSubmit={handleSubmit}
            disabled={submitDisabled}
          />

          <StatusPanel
            state={state}
            subtaskIndex={subtaskIndex}
            subtaskCount={subtaskCount}
          />

          <ControlPanel onControl={handleControl} state={state} />

          <LogPanel entries={logs} />
        </div>
      </div>

      <StatusBar
        vncConnected={vncConnected}
        agentState={state}
        vmResolution={vmResolution}
      />
    </div>
  );
}
