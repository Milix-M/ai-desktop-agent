import type { TaskDetail, TaskHistoryItem, TaskStatus, VmInfo, VmStatus } from "./types";

const BACKEND_URL =
  typeof window !== "undefined"
    ? `${window.location.protocol}//${window.location.hostname}:8081`
    : "http://localhost:8081";

export async function createTask(
  instruction: string,
  vmId?: string | null,
  allowVmRestart?: boolean
): Promise<TaskStatus> {
  const resp = await fetch(`${BACKEND_URL}/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instruction, vm_id: vmId ?? null, allow_vm_restart: allowVmRestart ?? false }),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

export async function getCurrentTask(): Promise<TaskStatus> {
  const resp = await fetch(`${BACKEND_URL}/tasks/current`);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

export async function controlTask(
  action: "pause" | "resume" | "stop"
): Promise<{ status: string }> {
  const resp = await fetch(
    `${BACKEND_URL}/tasks/current/${action}`,
    { method: "POST" }
  );
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

export function getWsUrl(): string {
  if (typeof window === "undefined") return "ws://localhost:8081/ws";
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.hostname}:8081/ws`;
}

export function getVncWsUrl(wsPort?: number): string {
  const port = wsPort ?? 6080;
  if (typeof window === "undefined") return `ws://localhost:${port}`;
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.hostname}:${port}`;
}

export async function getVms(): Promise<VmInfo[]> {
  const resp = await fetch(`${BACKEND_URL}/vms`);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

export async function createVm(name?: string): Promise<VmInfo> {
  const resp = await fetch(`${BACKEND_URL}/vms`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name ?? null }),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

export async function deleteVm(vmId: string): Promise<{ status: string }> {
  const resp = await fetch(`${BACKEND_URL}/vms/${vmId}`, { method: "DELETE" });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

export async function getVmStatus(): Promise<VmStatus> {
  const resp = await fetch(`${BACKEND_URL}/vm/status`);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

export async function restartVm(): Promise<VmStatus> {
  const resp = await fetch(`${BACKEND_URL}/vm/restart`, { method: "POST" });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

export async function getTaskHistory(limit = 20): Promise<TaskHistoryItem[]> {
  const resp = await fetch(`${BACKEND_URL}/tasks?limit=${limit}`);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

export async function getTaskDetail(taskId: string): Promise<TaskDetail> {
  const resp = await fetch(`${BACKEND_URL}/tasks/${taskId}`);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

export async function deleteTask(taskId: string): Promise<{ status: string }> {
  const resp = await fetch(`${BACKEND_URL}/tasks/${taskId}`, { method: "DELETE" });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}
