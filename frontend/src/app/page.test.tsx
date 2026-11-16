import { act, render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import Home from "./page";
import { controlTask } from "@/lib/api";

vi.mock("@/components/VncViewer", () => ({ default: () => null }));
vi.mock("@/components/VMControls", () => ({ default: () => null }));
const ws = vi.hoisted(() => ({ handler: null as null | ((message: import("@/lib/types").WsMessage) => void) }));
vi.mock("@/hooks/useWebSocket", () => ({ useWebSocket: (handler: typeof ws.handler) => { ws.handler = handler; } }));
vi.mock("@/lib/api", () => ({
  getVms: async () => [{ id: "desktop", name: "desktop", status: "running", kind: "container", ws_port: 6081 }],
  getCurrentTask: async () => ({ session_id: "running", vm_id: "desktop", state: "executing", is_running: true, subtasks: [], current_subtask_index: 0 }),
  getTaskHistory: async () => [{ id: "old", vm_id: "desktop", instruction: "old completed task", state: "completed", updated_at: 0 }],
  getTaskDetail: async () => ({ id: "old", vm_id: "desktop", instruction: "old completed task", state: "completed", subtasks: [], actions: [], current_subtask_index: 0, updated_at: 0 }),
  getVncWsUrl: () => "ws://localhost:6081",
  controlTask: vi.fn(), createTask: vi.fn(), deleteTask: vi.fn(),
  createVm: vi.fn(), deleteVm: vi.fn(), stopVm: vi.fn(), startVm: vi.fn(),
}));

afterEach(cleanup);

test("history keeps live controls available and receives live completion", async () => {
  render(<Home />);
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeEnabled());
  fireEvent.click(await screen.findByTitle("old completed task"));
  await screen.findByText("COMPLETED");
  expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
  expect(screen.getByRole("textbox")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "停止" }));
  expect(controlTask).toHaveBeenCalledWith("stop", "desktop");
  act(() => ws.handler?.({ type: "complete", vm_id: "desktop", success: true }));
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeDisabled());
  expect(screen.getByRole("textbox")).toBeEnabled();
});
