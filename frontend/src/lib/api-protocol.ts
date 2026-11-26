import type { TaskAction, TaskDetail, TaskSchedule, TaskHistoryItem, VmInfo, VmStatus } from "./types";

type RecordValue = Record<string, unknown>;
const record = (value: unknown): value is RecordValue => value !== null && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string";
const identifier = (value: unknown) => text(value) && value.length > 0;
const counter = (value: unknown) => Number.isSafeInteger(value) && (value as number) >= 0;
const nonnegative = (value: unknown) => typeof value === "number" && Number.isFinite(value) && value >= 0;
const bool = (value: unknown) => typeof value === "boolean";
const optional = (value: unknown, check: (value: unknown) => boolean) => value === undefined || check(value);
const nullableText = (value: unknown) => value === null || text(value);
const nullableId = (value: unknown) => value === null || identifier(value);
const port = (value: unknown) => counter(value) && (value as number) <= 65535;

function vmInfo(value: unknown): value is VmInfo {
  return record(value) && identifier(value.id) && text(value.name) && identifier(value.status) &&
    nullableText(value.health) && bool(value.managed) && text(value.vnc_host) &&
    port(value.vnc_port) && port(value.ws_port) && optional(value.kind, identifier) && optional(value.adb_port, port);
}

function taskSummary(value: unknown): value is TaskHistoryItem & RecordValue {
  return record(value) && identifier(value.id) && text(value.instruction) && identifier(value.state) &&
    (value.success === null || bool(value.success)) && nonnegative(value.updated_at) &&
    ["action_count", "success_count", "failure_count"].every(key => counter(value[key])) &&
    ["session_order", "prompt_tokens", "completion_tokens", "llm_calls", "decision_calls", "decision_escalations"]
      .every(key => optional(value[key], counter)) && optional(value.vm_id, nullableId) &&
    optional(value.stop_reason, nullableText) && optional(value.error_message, text);
}

function taskAction(value: unknown): value is TaskAction {
  return record(value) && identifier(value.action_type) && record(value.params) && text(value.description) &&
    bool(value.success) && text(value.error_message) && nonnegative(value.duration_ms) && nonnegative(value.at) &&
    text(value.reasoning) && nonnegative(value.confidence) && (value.confidence as number) <= 1;
}

function usage(value: unknown): boolean {
  return record(value) && ["requests", "input_tokens", "output_tokens"].every(key => optional(value[key], counter)) &&
    optional(value.estimated_cost_usd, nullableText) && optional(value.usage_complete, bool) &&
    optional(value.elapsed_seconds, nonnegative);
}

function taskDetail(value: unknown): value is TaskDetail {
  return taskSummary(value) && Array.isArray(value.actions) && value.actions.every(taskAction) &&
    Array.isArray(value.subtasks) && value.subtasks.every(task => record(task) && identifier(task.id) &&
      text(task.description) && text(task.expected_outcome)) && counter(value.current_subtask_index) &&
    nonnegative(value.created_at) && record(value.goal) && optional(value.execution_usage, usage);
}

function parseList<T extends { id: string }>(value: unknown, check: (value: unknown) => value is T, message: string): T[] {
  if (!Array.isArray(value) || !value.every(check) || new Set(value.map(item => item.id)).size !== value.length) {
    throw new Error(message);
  }
  return value;
}

export function parseVms(value: unknown): VmInfo[] {
  return parseList(value, vmInfo, "環境一覧の応答が不正です");
}

export function parseVmInfo(value: unknown, expectedId: string): VmInfo {
  if (!vmInfo(value) || value.id !== expectedId) throw new Error("環境の応答が不正です");
  return value;
}

export function parseVmStatus(value: unknown): VmStatus {
  if (!record(value) || !bool(value.running) || !identifier(value.status) || !nullableText(value.health) || !nullableText(value.name)) {
    throw new Error("環境状態の応答が不正です");
  }
  return value as unknown as VmStatus;
}

export function parseTaskHistory(value: unknown): TaskHistoryItem[] {
  return parseList(value, taskSummary, "履歴の応答が不正です");
}

export function parseTaskDetail(value: unknown, expectedId: string): TaskDetail {
  if (!taskDetail(value) || value.id !== expectedId) throw new Error("タスク詳細の応答が不正です");
  return value;
}

export function parseLogin(value: unknown): { authenticated: boolean } {
  if (!record(value) || value.authenticated !== true) throw new Error("認証の応答が不正です");
  return value as { authenticated: boolean };
}

export function parseAcknowledgement(value: unknown, status: string): { status: string } {
  if (!record(value) || value.status !== status) throw new Error("操作の応答が不正です。状態を確認してください");
  return value as { status: string };
}

function taskSchedule(value: unknown): value is TaskSchedule {
  return record(value) && identifier(value.id) &&
    ((identifier(value.vm_id) && value.vm_kind == null) ||
      (value.vm_id === null && (value.vm_kind === "container" || value.vm_kind === "qemu" || value.vm_kind === "android"))) &&
    text(value.instruction) && optional(value.preparing, bool) &&
    counter(value.run_at) && (value.interval === 0 || value.interval === 86400 || value.interval === 604800) &&
    (value.next_run === null || counter(value.next_run)) && nullableId(value.last_task_id) && nullableText(value.last_error);
}

export function parseSchedules(value: unknown) {
  return parseList(value, taskSchedule, "予約一覧の応答が不正です");
}

export function parseSchedule(value: unknown, expectedId: string) {
  if (!taskSchedule(value) || value.id !== expectedId) throw new Error("予約の受付結果を確認できません");
  return value;
}
