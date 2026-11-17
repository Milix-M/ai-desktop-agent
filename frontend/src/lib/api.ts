import type { TaskDetail, TaskHistoryItem, TaskStatus, VmInfo, VmStatus } from "./types";

const BACKEND_URL =
  typeof window !== "undefined"
    ? `${window.location.protocol}//${window.location.hostname}:8081`
    : "http://localhost:8081";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${BACKEND_URL}${path}`;
  const response = await fetch(url, { ...init, credentials: "include" });
  if (!response.ok) {
    if (response.status === 401 && typeof window !== "undefined" && !path.startsWith("/auth/")) {
      window.dispatchEvent(new Event("auth-required"));
    }
    let detail = "";
    try {
      const body: unknown = await response.json();
      if (body && typeof body === "object" && "detail" in body && typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // Non-JSON responses (for example a proxy error) still have a useful status.
    }
    throw new Error(detail ? `${detail} (HTTP ${response.status})` : `HTTP ${response.status}`);
  }
  return response.json();
}

function post(body?: object): RequestInit {
  return body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : { method: "POST" };
}

export function createTask(instruction: string, vmId?: string | null, allowVmRestart?: boolean): Promise<TaskStatus> {
  return request("/tasks", post({ instruction, vm_id: vmId ?? null, allow_vm_restart: allowVmRestart ?? false }));
}

export function getCurrentTask(vmId?: string | null): Promise<TaskStatus> {
  return request(`/tasks/current${vmId ? `?vm_id=${encodeURIComponent(vmId)}` : ""}`);
}

export function controlTask(action: "pause" | "resume" | "stop", vmId?: string | null, manualInputId?: string): Promise<{ status: string }> {
  const body = action === "resume" && manualInputId
    ? { manual_input_id: manualInputId, allow_screen_share: true }
    : undefined;
  return request(`/tasks/current/${action}${vmId ? `?vm_id=${encodeURIComponent(vmId)}` : ""}`, post(body));
}

export function getWsUrl(): string {
  if (typeof window === "undefined") return "ws://localhost:8081/ws";
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.hostname}:8081/ws`;
}

export function getVncWsUrl(vmId = "vm"): string {
  if (typeof window === "undefined") return `ws://localhost:8081/vnc/${encodeURIComponent(vmId)}`;
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.hostname}:8081/vnc/${encodeURIComponent(vmId)}`;
}

export function getAuthStatus(): Promise<{ authenticated: boolean }> {
  return request("/auth/status");
}

export function login(key: string): Promise<{ authenticated: boolean }> {
  return request("/auth/login", post({ key }));
}

export function logout(): Promise<{ status: string }> {
  return request("/auth/logout", post());
}

export function getVms(): Promise<VmInfo[]> {
  return request("/vms");
}

export function createVm(name?: string, kind?: string): Promise<VmInfo> {
  return request("/vms", post({ name: name ?? null, kind: kind ?? null }));
}

export function deleteVm(vmId: string): Promise<{ status: string }> {
  return request(`/vms/${encodeURIComponent(vmId)}`, { method: "DELETE" });
}

export function stopVm(vmId: string): Promise<VmInfo> {
  return request(`/vms/${encodeURIComponent(vmId)}/stop`, post());
}

export function startVm(vmId: string): Promise<VmInfo> {
  return request(`/vms/${encodeURIComponent(vmId)}/start`, post());
}

export function getVmStatus(): Promise<VmStatus> {
  return request("/vm/status");
}

export function restartVm(): Promise<VmStatus> {
  return request("/vm/restart", post());
}

export function getTaskHistory(limit = 20): Promise<TaskHistoryItem[]> {
  return request(`/tasks?limit=${limit}`);
}

export function getTaskDetail(taskId: string): Promise<TaskDetail> {
  return request(`/tasks/${encodeURIComponent(taskId)}`);
}

export function deleteTask(taskId: string): Promise<{ status: string }> {
  return request(`/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" });
}
