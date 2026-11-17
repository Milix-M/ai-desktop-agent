export interface SubtaskInfo {
  id: string;
  description: string;
}

export interface PendingSubmission {
  requestId: string;
  instruction: string;
  vmId: string | null;
  allowVmRestart: boolean;
  gone?: boolean;
}

export interface TaskStatus {
  server_id?: string;
  session_id: string | null;
  state: string;
  is_running: boolean;
  action_count: number;
  success_count: number;
  failure_count: number;
  subtasks: SubtaskInfo[];
  current_subtask_index: number;
  vm_id?: string | null;
  revision?: number;
  session_order?: number;
  manual_input?: ManualInputRequest | null;
  replayed?: boolean;
}

export interface ManualInputRequest {
  id: string;
  reason: string;
}

export interface VmInfo {
  id: string;
  name: string;
  status: string;
  health: string | null;
  vnc_port: number;
  ws_port: number;
  vnc_host: string;
  managed: boolean;
  kind?: string; // qemu | container | android（旧APIでは欠落）
  adb_port?: number; // 旧android（Redroid）互換。現行は未使用
}

export interface WsStateMessage {
  type: "state";
  session_id?: string | null;
  vm_id?: string | null;
  state: string;
  subtask_index: number;
  subtask_count: number;
  action_count: number;
  subtasks?: SubtaskInfo[];
  revision?: number;
  session_order?: number;
  manual_input?: ManualInputRequest | null;
}

export interface WsActionMessage {
  type: "action";
  session_id?: string | null;
  session_order?: number;
  vm_id?: string | null;
  action_type: string;
  description: string;
  success: boolean;
}

export interface WsErrorMessage {
  type: "error";
  session_id?: string | null;
  session_order?: number;
  vm_id?: string | null;
  message: string;
}

export interface WsCompleteMessage {
  type: "complete";
  session_id?: string | null;
  vm_id?: string | null;
  success: boolean;
  revision?: number;
  session_order?: number;
}

export type WsMessage = (
  | { type: "snapshot"; tasks: TaskStatus[]; through_session_order?: number }
  | WsStateMessage
  | WsActionMessage
  | WsErrorMessage
  | WsCompleteMessage
  | { type: "status"; state: string }
  | { type: "pong" }
) & { server_id?: string };

export interface LogEntry {
  id: number;
  time: string;
  message: string;
  level: "action" | "error" | "state" | "complete";
}

export interface VmStatus {
  running: boolean;
  status: string;
  health: string | null;
  name: string | null;
}

export interface TaskHistoryItem {
  id: string;
  instruction: string;
  state: string;
  success: boolean | null;
  action_count: number;
  success_count: number;
  failure_count: number;
  updated_at: number;
  session_order?: number;
  vm_id?: string | null;
  prompt_tokens?: number;
  completion_tokens?: number;
  llm_calls?: number;
  decision_calls?: number;
  decision_escalations?: number;
  stop_reason?: string | null;
  error_message?: string;
}

export interface TaskAction {
  action_type: string;
  params: Record<string, unknown>;
  description: string;
  success: boolean;
  error_message: string;
  duration_ms: number;
  at: number;
  reasoning: string;
  confidence: number;
}

export interface TaskDetail extends TaskHistoryItem {
  execution_usage?: {
    requests?: number;
    input_tokens?: number;
    output_tokens?: number;
    estimated_cost_usd?: string | null;
    usage_complete?: boolean;
    elapsed_seconds?: number;
  };
  actions: TaskAction[];
  subtasks: { id: string; description: string; expected_outcome: string }[];
  current_subtask_index: number;
  goal: Record<string, unknown>;
  created_at: number;
}
