export interface TaskStatus {
  session_id: string | null;
  state: string;
  is_running: boolean;
  action_count: number;
  success_count: number;
  failure_count: number;
}

export interface WsStateMessage {
  type: "state";
  state: string;
  subtask_index: number;
  subtask_count: number;
  action_count: number;
}

export interface WsActionMessage {
  type: "action";
  action_type: string;
  description: string;
  success: boolean;
}

export interface WsErrorMessage {
  type: "error";
  message: string;
}

export interface WsCompleteMessage {
  type: "complete";
  success: boolean;
}

export type WsMessage =
  | WsStateMessage
  | WsActionMessage
  | WsErrorMessage
  | WsCompleteMessage
  | { type: "status"; state: string }
  | { type: "pong" };

export interface LogEntry {
  id: number;
  time: string;
  message: string;
  level: "action" | "error" | "state" | "complete";
}
