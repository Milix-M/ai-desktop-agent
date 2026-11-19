import { afterEach, expect, test, vi } from "vitest";
import { getTaskDetail, getTaskHistory, getVms, getVmStatus, restartVm, startVm, stopVm } from "./api";

const vm = { id: "desktop", name: "作業環境", status: "running", health: "healthy", kind: "container",
  managed: false, vnc_host: "desktop", vnc_port: 5900, ws_port: 0 };
const summary = { id: "saved", instruction: "売上報告を保存する", state: "completed", success: true,
  action_count: 1, success_count: 1, failure_count: 0, updated_at: 1700000000, vm_id: "desktop" };
const action = { action_type: "left_click", params: { x: 20, y: 30 }, description: "保存先を開く",
  success: true, error_message: "", duration_ms: 10, at: 1700000000, reasoning: "保存ボタンが見える", confidence: 0.9 };
const detail = { ...summary, actions: [action], subtasks: [{ id: "save", description: "保存する", expected_outcome: "報告書がある" }],
  current_subtask_index: 1, created_at: 1699999990, goal: { description: "報告書を保存" } };
const status = { running: true, status: "running", health: null, name: "作業環境" };
const response = (body: unknown) => ({ ok: true, status: 200, json: async () => body }) as Response;
afterEach(() => vi.restoreAllMocks());

test.each([null, {}, "environments", [null], [vm, { ...vm, id: "other", managed: "false" }],
  [vm, { ...vm, name: "重複した環境" }], [{ ...vm, name: {} }], [{ ...vm, status: null }],
  [{ ...vm, health: true }], [{ ...vm, vnc_port: -1 }], [{ ...vm, ws_port: 1.5 }],
  [{ ...vm, vnc_host: [] }], [{ ...vm, kind: {} }], [{ ...vm, adb_port: "5555" }],
  [{ ...vm, id: "" }], [{ ...vm, managed: null }], [{ ...vm, vnc_port: 65536 }],
].map(value => [value] as const))("invalid environment lists cannot replace the selected workspace: %j", async value => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(response(value));
  await expect(getVms()).rejects.toThrow("環境一覧の応答が不正です");
});

test.each([null, {}, "history", [null], [summary, { ...summary }],
  [{ ...summary, instruction: {} }], [{ ...summary, id: "" }], [{ ...summary, state: false }],
  [{ ...summary, success: "false" }], [{ ...summary, action_count: -1 }],
  [{ ...summary, updated_at: "yesterday" }], [{ ...summary, llm_calls: true }],
  [{ ...summary, vm_id: 3 }], [{ ...summary, error_message: {} }],
  [{ ...summary, stop_reason: {} }], [{ ...summary, vm_id: "" }],
].map(value => [value] as const))("invalid history is rejected atomically before rendering or deletion: %j", async value => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(response(value));
  await expect(getTaskHistory()).rejects.toThrow("履歴の応答が不正です");
});

test.each([null, {}, { ...detail, id: "different-task" }, { ...detail, actions: null },
  { ...detail, actions: [null] }, { ...detail, actions: [{ ...action, reasoning: {} }] },
  { ...detail, actions: [{ ...action, success: "false" }] },
  { ...detail, actions: [{ ...action, params: [] }] },
  { ...detail, subtasks: [null] }, { ...detail, current_subtask_index: -1 },
  { ...detail, goal: [] }, { ...detail, execution_usage: null },
  { ...detail, execution_usage: { requests: "one" } },
  { ...detail, execution_usage: { usage_complete: "false" } },
  { ...detail, actions: [{ ...action, action_type: "" }] },
  { ...detail, actions: [{ ...action, description: {} }] },
  { ...detail, actions: [{ ...action, error_message: null }] },
  { ...detail, actions: [{ ...action, duration_ms: -1 }] },
  { ...detail, actions: [{ ...action, at: Infinity }] },
  { ...detail, actions: [{ ...action, confidence: 1.1 }] },
  { ...detail, actions: [{ ...action, confidence: "0.9" }] },
  { ...detail, subtasks: [{ id: "", description: "保存", expected_outcome: "保存済み" }] },
  { ...detail, subtasks: [{ id: "save", description: {}, expected_outcome: "保存済み" }] },
  { ...detail, subtasks: [{ id: "save", description: "保存", expected_outcome: null }] },
  { ...detail, created_at: -1 },
  { ...detail, execution_usage: { estimated_cost_usd: {} } },
  { ...detail, execution_usage: { elapsed_seconds: -1 } },
])("invalid task details cannot replace a readable task: %j", async value => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(response(value));
  await expect(getTaskDetail("saved")).rejects.toThrow("タスク詳細の応答が不正です");
});

test.each(["start", "stop"] as const)("%s cannot acknowledge an operation with another environment's response", async method => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ ...vm, id: "other" }));
  await expect((method === "start" ? startVm : stopVm)("desktop")).rejects.toThrow("環境の応答が不正です");
});

test.each([null, {}, { ...status, running: "false" }, { ...status, health: {} }, { ...status, name: false }, { ...status, status: "" }])(
  "invalid default-environment status cannot confirm readiness or restart: %j", async value => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response(value));
    await expect(getVmStatus()).rejects.toThrow("環境状態の応答が不正です");
    await expect(restartVm()).rejects.toThrow("環境状態の応答が不正です");
  },
);

test("a malformed scoped environment result is rejected before reporting an operation result", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(response(null));
  await expect(startVm("desktop")).rejects.toThrow("環境の応答が不正です");
});

test("valid optional usage and environment metadata are retained, including stopped history", async () => {
  const legacy = { ...summary, vm_id: null, success: null, stop_reason: null, error_message: "",
    session_order: 1, prompt_tokens: 2, completion_tokens: 3, llm_calls: 4, decision_calls: 5, decision_escalations: 6 };
  const usage = { requests: 4, input_tokens: 2, output_tokens: 3, estimated_cost_usd: "0.01", usage_complete: true, elapsed_seconds: 0.5 };
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(response([{ ...vm, kind: undefined, adb_port: 5555 }]))
    .mockResolvedValueOnce(response([legacy])).mockResolvedValueOnce(response({ ...detail, execution_usage: usage }))
    .mockResolvedValueOnce(response({ ...detail, execution_usage: { estimated_cost_usd: null } }));
  expect(await getVms()).toEqual([{ ...vm, kind: undefined, adb_port: 5555 }]);
  expect(await getTaskHistory()).toEqual([legacy]);
  expect((await getTaskDetail("saved")).execution_usage).toEqual(usage);
  expect((await getTaskDetail("saved")).execution_usage).toEqual({ estimated_cost_usd: null });
  expect(fetch).toHaveBeenCalledTimes(4);
});

test("valid records retain unknown fields, optional legacy metadata and scoped operation results", async () => {
  const fetch = vi.spyOn(globalThis, "fetch");
  fetch.mockResolvedValueOnce(response([{ ...vm, annotation: "preserved" }]))
    .mockResolvedValueOnce(response([summary])).mockResolvedValueOnce(response(detail))
    .mockResolvedValueOnce(response(vm)).mockResolvedValueOnce(response(vm))
    .mockResolvedValueOnce(response(status)).mockResolvedValueOnce(response(status));
  expect(await getVms()).toEqual([{ ...vm, annotation: "preserved" }]);
  expect(await getTaskHistory()).toEqual([summary]);
  expect(await getTaskDetail("saved")).toEqual(detail);
  expect(await startVm("desktop")).toEqual(vm);
  expect(await stopVm("desktop")).toEqual(vm);
  expect(await getVmStatus()).toEqual(status);
  expect(await restartVm()).toEqual(status);
});
