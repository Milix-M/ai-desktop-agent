import { expect, test, vi } from "vitest";
import { parseWsMessage } from "./task-protocol";

const task = { server_id: "server-1", session_id: "task-1", vm_id: "desktop", revision: 3, session_order: 1,
  state: "awaiting_user", is_running: true, action_count: 1, success_count: 1, failure_count: 0,
  subtasks: [{ id: "step-1", description: "文書を保存する" }], current_subtask_index: 0,
  manual_input: { id: "consent-1", reason: "保存先を確認してください" }, replayed: false };
const snapshot = { type: "snapshot", server_id: "server-1", through_session_order: 1, tasks: [task] };
const state = { type: "state", server_id: "server-1", session_id: "task-1", vm_id: "desktop",
  state: "executing", subtask_index: 0, subtask_count: 1, action_count: 0 };
const withTask = (patch: Record<string, unknown>) => ({ ...snapshot, tasks: [{ ...task, ...patch }] });

test.each([
  snapshot,
  { type: "snapshot", tasks: [] },
  { type: "snapshot", tasks: [{ ...task, server_id: undefined, vm_id: undefined, revision: undefined,
    session_order: undefined, manual_input: undefined, replayed: true }] },
  { type: "snapshot", tasks: ["desktop", "vm"].map(vm_id => ({ ...task, session_id: null, vm_id,
    is_running: false, state: "idle", manual_input: null })) },
  { type: "snapshot", tasks: [1, 2].map(() => ({ ...task, session_id: null, vm_id: null,
    is_running: false, state: "idle", manual_input: null })) },
  state,
  { ...state, subtasks: task.subtasks, revision: 3, session_order: 1, manual_input: null },
  { ...state, state: "awaiting_user", manual_input: task.manual_input },
  { type: "action", action_type: "left_click", description: "", success: true },
  { type: "action", session_id: null, vm_id: null, action_type: "future_action", description: "操作", success: false },
  { type: "error", message: "" },
  { type: "complete", success: false },
  { type: "status", state: "connected" },
  { type: "pong" },
  { ...snapshot, future_metadata: { diagnostics: [1, 2] } },
])("accepts supported and legacy wire messages: %j", value => {
  const raw = JSON.stringify(value);
  expect(parseWsMessage(raw)).toEqual(JSON.parse(raw));
});

test.each(["idle", "understanding", "planning", "executing", "waiting", "verifying", "recovering",
  "paused", "awaiting_user", "completed", "failed", "interrupted"])("accepts the %s task state", value => {
  expect(parseWsMessage(JSON.stringify({ ...state, state: value }))).toMatchObject({ state: value });
});

test.each(["null", "true", "7", '"state"', "[]", "{}", "{truncated", "1e999"])(
  "rejects malformed JSON or a non-message JSON value: %s", raw => {
    expect(() => parseWsMessage(raw)).toThrow();
  },
);

test("never coerces a binary or object payload to a string", () => {
  const toString = vi.fn(() => JSON.stringify(snapshot));
  expect(() => parseWsMessage({ toString })).toThrow("Invalid task-state message");
  expect(toString).not.toHaveBeenCalled();
  expect(() => parseWsMessage(new Uint8Array([123, 125]))).toThrow();
});

const invalid = [
  ["unknown type", { type: "future_state_event" }],
  ["invalid server identity", { ...snapshot, server_id: null }],
  ["empty server identity", { ...snapshot, server_id: "" }],
  ["missing task collection", { type: "snapshot" }],
  ["wrong task collection", { ...snapshot, tasks: {} }],
  ["null task", { ...snapshot, tasks: [null] }],
  ["array task", { ...snapshot, tasks: [[]] }],
  ["missing task identity", withTask({ session_id: undefined })],
  ["empty task identity", withTask({ session_id: "" })],
  ["invalid task identity", withTask({ session_id: {} })],
  ["invalid environment identity", withTask({ vm_id: 1 })],
  ["empty environment identity", withTask({ vm_id: "" })],
  ["invalid nested server", withTask({ server_id: 7 })],
  ["wrong running flag", withTask({ is_running: "false" })],
  ["running without identity", withTask({ session_id: null, manual_input: null })],
  ["negative action counter", withTask({ action_count: -1 })],
  ["fractional action counter", withTask({ action_count: 0.5 })],
  ["wrong success counter", withTask({ success_count: "1" })],
  ["wrong failure counter", withTask({ failure_count: null })],
  ["negative subtask position", withTask({ current_subtask_index: -1 })],
  ["invalid revision", withTask({ revision: null })],
  ["unsafe order", withTask({ session_order: Number.MAX_SAFE_INTEGER + 1 })],
  ["wrong subtasks", withTask({ subtasks: {} })],
  ["null subtask", withTask({ subtasks: [null] })],
  ["empty subtask identity", withTask({ subtasks: [{ id: "", description: "保存する" }] })],
  ["non-text subtask", withTask({ subtasks: [{ id: "step", description: {} }] })],
  ["unknown task state", withTask({ state: "new_state" })],
  ["non-text task state", withTask({ state: [] })],
  ["wrong replay flag", withTask({ replayed: "false" })],
  ["invalid manual request", withTask({ manual_input: [] })],
  ["empty manual identity", withTask({ manual_input: { id: "", reason: "確認する" } })],
  ["non-text manual reason", withTask({ manual_input: { id: "consent", reason: {} } })],
  ["manual request outside handoff", withTask({ state: "executing" })],
  ["manual request without environment", withTask({ vm_id: null })],
  ["manual request on stopped task", withTask({ is_running: false })],
  ["invalid snapshot fence", { ...snapshot, through_session_order: -1 }],
  ["duplicate environment", { ...snapshot, tasks: [task, { ...task, session_id: "task-2" }] }],
  ["duplicate task", { ...snapshot, tasks: [task, { ...task, vm_id: "vm" }] }],
  ["mixed backend generations", withTask({ server_id: "server-2" })],
  ["mixed nested generations", { type: "snapshot", tasks: [task,
    { ...task, server_id: "server-2", session_id: "task-2", vm_id: "vm" }] }],
  ["invalid state identity", { ...state, session_id: {} }],
  ["invalid state value", { ...state, state: null }],
  ["invalid state position", { ...state, subtask_index: "0" }],
  ["invalid state count", { ...state, subtask_count: false }],
  ["invalid state action count", { ...state, action_count: null }],
  ["invalid optional subtasks", { ...state, subtasks: false }],
  ["unbound manual request", { ...state, state: "awaiting_user", session_id: null, manual_input: task.manual_input }],
  ["empty action name", { type: "action", action_type: "", description: "", success: true }],
  ["invalid action identity", { type: "action", session_id: {}, action_type: "wait", description: "", success: true }],
  ["wrong action description", { type: "action", action_type: "wait", description: {}, success: true }],
  ["wrong action outcome", { type: "action", action_type: "wait", description: "", success: "false" }],
  ["non-text error", { type: "error", message: {} }],
  ["invalid error identity", { type: "error", message: "error", vm_id: {} }],
  ["invalid completion identity", { type: "complete", success: true, vm_id: {} }],
  ["wrong completion outcome", { type: "complete", success: "false" }],
  ["empty status", { type: "status", state: "" }],
  ["invalid heartbeat server", { type: "pong", server_id: {} }],
] as const;

test.each(invalid)("rejects %s before the consumer can apply it", (_name, value) => {
  expect(() => parseWsMessage(JSON.stringify(value))).toThrow("Invalid task-state message");
});
