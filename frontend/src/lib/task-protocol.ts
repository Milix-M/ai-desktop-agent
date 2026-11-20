import type { TaskStatus, WsMessage } from "./types";

type RecordValue = Record<string, unknown>;
const record = (value: unknown): value is RecordValue => value !== null && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string";
const identifier = (value: unknown): value is string => text(value) && value.length > 0;
const counter = (value: unknown) => Number.isSafeInteger(value) && (value as number) >= 0;
const optional = (value: unknown, check: (value: unknown) => boolean) => value === undefined || check(value);
const nullableId = (value: unknown) => value === null || optional(value, identifier);
const state = (value: unknown) => ["idle", "understanding", "planning", "executing", "waiting", "verifying",
  "recovering", "paused", "awaiting_user", "completed", "failed", "interrupted"].includes(value as string);

function identity(value: RecordValue): boolean {
  return nullableId(value.session_id) && nullableId(value.vm_id) &&
    optional(value.revision, counter) && optional(value.session_order, counter);
}

function subtasks(value: unknown): boolean {
  return Array.isArray(value) && value.every(task => record(task) && identifier(task.id) && text(task.description));
}

function manualInput(value: RecordValue): boolean {
  const input = value.manual_input;
  return input === undefined || input === null || (record(input) && identifier(input.id) && text(input.reason) &&
    value.state === "awaiting_user" && identifier(value.session_id) && identifier(value.vm_id) &&
    (value.is_running === undefined || value.is_running === true));
}

function taskStatus(value: unknown): value is TaskStatus {
  return record(value) && optional(value.server_id, identifier) && identity(value) &&
    (value.session_id === null || identifier(value.session_id)) && state(value.state) &&
    typeof value.is_running === "boolean" && (!value.is_running || identifier(value.session_id)) &&
    counter(value.action_count) && counter(value.success_count) && counter(value.failure_count) &&
    subtasks(value.subtasks) && counter(value.current_subtask_index) && manualInput(value) &&
    optional(value.replayed, value => typeof value === "boolean");
}

function unique(values: (string | null | undefined)[]): boolean {
  const ids = values.filter(value => value !== null && value !== undefined);
  return new Set(ids).size === ids.length;
}

function coherentTasks(tasks: TaskStatus[], serverId: unknown): boolean {
  const servers = [serverId, ...tasks.map(task => task.server_id)].filter(value => value !== undefined);
  return unique(tasks.map(task => task.vm_id)) && unique(tasks.map(task => task.session_id)) && new Set(servers).size <= 1;
}

function message(value: unknown): value is WsMessage {
  if (!record(value) || !optional(value.server_id, identifier)) return false;
  switch (value.type) {
    case "snapshot":
      return optional(value.through_session_order, counter) && Array.isArray(value.tasks) &&
        value.tasks.every(taskStatus) && coherentTasks(value.tasks, value.server_id);
    case "state":
      return identity(value) && state(value.state) && counter(value.subtask_index) && counter(value.subtask_count) &&
        counter(value.action_count) && optional(value.subtasks, subtasks) && manualInput(value);
    case "action":
      return identity(value) && identifier(value.action_type) && text(value.description) && typeof value.success === "boolean";
    case "error":
      return identity(value) && text(value.message);
    case "complete":
      return identity(value) && typeof value.success === "boolean";
    case "status":
      return identifier(value.state);
    case "pong":
      return true;
    default:
      return false;
  }
}

export function parseWsMessage(raw: unknown): WsMessage {
  if (typeof raw !== "string") throw new Error("Invalid task-state message");
  const value: unknown = JSON.parse(raw);
  if (!message(value)) throw new Error("Invalid task-state message");
  return value;
}

export function parseTaskStatus(value: unknown, vmId?: string | null): TaskStatus {
  if (!taskStatus(value) || (vmId != null && value.vm_id !== vmId)) throw new Error("タスクの応答が不正です");
  return value;
}
