import type { ScheduleEnvironmentKind, TaskDetail, TaskHistoryItem, TaskStatus, VmInfo, VmStatus } from "./types";
import { parseTaskStatus } from "./task-protocol";
import { parseAcknowledgement, parseLogin, parseSchedule, parseSchedules, parseTaskDetail, parseTaskHistory, parseVmInfo, parseVms, parseVmStatus } from "./api-protocol";
import { forgetVmCreation, vmCreationRetry, type CreationKind } from "./vm-creation-retries";

const BACKEND_URL =
  typeof window !== "undefined"
    ? `${window.location.protocol}//${window.location.hostname}:8081`
    : "http://localhost:8081";

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) { super(message); }
}

let authVersion = 0;
let authChanges = 0;
let authQueue: Promise<void> = Promise.resolve();
let authCheck: { version: number; promise: Promise<boolean | null> } | null = null;
const AUTH_CHANGE_KEY = "ai-desktop-agent-auth-change";

function authStamp(): string | null {
  try { return localStorage.getItem(AUTH_CHANGE_KEY); }
  catch { return null; }
}

function changeAuthStamp() {
  authVersion++;
  try { localStorage.setItem(AUTH_CHANGE_KEY, crypto.getRandomValues(new Uint32Array(4)).join("-")); }
  catch { /* Storage may be disabled; the current tab still has its version guard. */ }
}

async function changeAuthentication<T>(change: (signal: AbortSignal) => Promise<T>): Promise<T> {
  changeAuthStamp();
  authChanges++;
  const operation = authQueue.then(async () => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try { return await change(controller.signal); }
    catch (error) {
      if (controller.signal.aborted) throw new Error("認証の応答がありません。接続を確認してもう一度操作してください", { cause: error });
      throw error;
    } finally { clearTimeout(timer); }
  });
  authQueue = operation.then(() => {}, () => {});
  try { return await operation; }
  finally { authChanges--; changeAuthStamp(); }
}

export async function confirmAuthentication(isCurrent: () => boolean = () => true): Promise<void> {
  if (authChanges || !isCurrent()) return;
  const version = authVersion;
  const stamp = authStamp();
  let pending = authCheck;
  if (!pending || pending.version !== version) {
    const check = { version, promise: getAuthStatus().then(result => result.authenticated, () => null) };
    authCheck = check;
    void check.promise.then(() => { if (authCheck === check) authCheck = null; });
    pending = check;
  }
  const authenticated = await pending.promise;
  if (authenticated === false && !authChanges && version === authVersion && stamp === authStamp() && isCurrent()) {
    authVersion++;
    window.dispatchEvent(new Event("auth-required"));
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const callerSignal = init?.signal;
  const controller = new AbortController();
  const signal = controller.signal;
  const cancel = () => controller.abort();
  if (callerSignal?.aborted) cancel();
  callerSignal?.addEventListener("abort", cancel, { once: true });
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; cancel(); }, 30000);
  const checkCancelled = () => {
    if (signal.aborted) throw new DOMException("aborted", "AbortError");
  };
  const version = authVersion;
  const stamp = authStamp();
  const url = `${BACKEND_URL}${path}`;
  try {
    checkCancelled();
    const response = await fetch(url, { ...init, signal, credentials: "include" });
    checkCancelled();
    if (!response.ok) {
      if (response.status === 401 && typeof window !== "undefined" && !path.startsWith("/auth/")) {
        await confirmAuthentication(() => !signal.aborted && version === authVersion && stamp === authStamp());
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
      checkCancelled();
      throw new ApiError(response.status, detail ? `${detail} (HTTP ${response.status})` : `HTTP ${response.status}`);
    }
    const result = await response.json();
    checkCancelled();
    return result;
  } catch (error) {
    if (timedOut && !callerSignal?.aborted) {
      throw new Error("応答を確認できません。状態を確認してからもう一度操作してください", { cause: error });
    }
    throw error;
  } finally {
    clearTimeout(timer);
    callerSignal?.removeEventListener("abort", cancel);
  }
}

function post(body?: object): RequestInit {
  return body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : { method: "POST" };
}

export async function createTask(instruction: string, vmId?: string | null, allowVmRestart?: boolean, requestId?: string): Promise<TaskStatus> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30000);
  const init = post({ instruction, vm_id: vmId ?? null, allow_vm_restart: allowVmRestart ?? false });
  if (requestId) init.headers = { ...init.headers, "Idempotency-Key": requestId };
  try {
    return parseTaskStatus(await request<unknown>("/tasks", { ...init, signal: controller.signal }), vmId);
  } catch (error) {
    if (error instanceof ApiError || !requestId) throw error;
    throw new Error("通信エラーで受付結果を確認できません。同じ内容で再送してください", { cause: error });
  } finally { clearTimeout(timer); }
}

export async function getCurrentTask(vmId?: string | null): Promise<TaskStatus> {
  return parseTaskStatus(await request<unknown>(`/tasks/current${vmId ? `?vm_id=${encodeURIComponent(vmId)}` : ""}`), vmId);
}

export async function controlTask(action: "pause" | "resume" | "stop", vmId: string, sessionId: string, manualInputId?: string): Promise<{ status: string }> {
  const body = action === "resume" && manualInputId
    ? { manual_input_id: manualInputId, allow_screen_share: true }
    : undefined;
  return parseAcknowledgement(await request<unknown>(`/tasks/current/${action}?vm_id=${encodeURIComponent(vmId)}&session_id=${encodeURIComponent(sessionId)}`, post(body)),
    { pause: "paused", resume: "resumed", stop: "stopped" }[action]);
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

export async function getAuthStatus(signal?: AbortSignal): Promise<{ authenticated: boolean }> {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  if (signal?.aborted) cancel();
  signal?.addEventListener("abort", cancel, { once: true });
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
    while (!controller.signal.aborted) {
      const version = authVersion;
      const stamp = authStamp();
      try {
        const status = await request<unknown>("/auth/status", { signal: controller.signal, cache: "no-store" });
        if (controller.signal.aborted) break;
        if (!status || typeof status !== "object" || !("authenticated" in status) || typeof status.authenticated !== "boolean") {
          throw new Error("認証の応答が不正です");
        }
        if (version === authVersion && stamp === authStamp()) return { authenticated: status.authenticated };
      } catch (error) {
        if (controller.signal.aborted || (version === authVersion && stamp === authStamp())) throw error;
      }
    }
    throw new DOMException("認証の確認が中断されました", "AbortError");
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
  }
}

export function login(key: string): Promise<{ authenticated: boolean }> {
  return changeAuthentication(async signal => parseLogin(await request<unknown>("/auth/login", { ...post({ key }), signal })));
}

export function logout(): Promise<{ status: string }> {
  return changeAuthentication(async signal => parseAcknowledgement(await request<unknown>("/auth/logout", { ...post(), signal }), "logged_out"));
}

export async function getVms(): Promise<VmInfo[]> {
  return parseVms(await request<unknown>("/vms"));
}

interface VmCreation {
  id: string;
  vm_id: string;
  state: "queued" | "running" | "succeeded" | "failed";
  result: VmInfo | null;
  error: string | null;
  error_status: number | null;
}

function creationResponse(value: unknown, kind: CreationKind): VmCreation {
  if (!value || typeof value !== "object") throw new Error("作成要求の応答が不正です");
  const job = value as VmCreation;
  if (!/^creation-[0-9a-f]{64}$/.test(job.id) || typeof job.vm_id !== "string" || !job.vm_id ||
    !["queued", "running", "succeeded", "failed"].includes(job.state)) throw new Error("作成要求の応答が不正です");
  if (job.state === "succeeded") {
    const result = parseVmInfo(job.result, job.vm_id);
    if (result.kind !== kind || result.managed !== true) throw new Error("作成結果が不正です");
  }
  if (job.state === "failed" && (typeof job.error !== "string" || ![400, 409, 503].includes(job.error_status ?? 0))) {
    throw new Error("作成エラーの応答が不正です");
  }
  return job;
}

async function creationRequest(path: string, init: RequestInit, signal: AbortSignal): Promise<unknown> {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  signal.addEventListener("abort", cancel, { once: true });
  if (signal.aborted) controller.abort();
  const timer = setTimeout(cancel, 10000);
  try { return await request(path, { ...init, signal: controller.signal }); }
  finally { clearTimeout(timer); signal.removeEventListener("abort", cancel); }
}

function waitForCreation(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const cancel = () => { clearTimeout(timer); signal.removeEventListener("abort", cancel); reject(new DOMException("aborted", "AbortError")); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", cancel); resolve(); }, 500);
    signal.addEventListener("abort", cancel, { once: true });
    if (signal.aborted) cancel();
  });
}

export async function createVm(name?: string, kind?: string, signal?: AbortSignal): Promise<VmInfo> {
  if (signal?.aborted) throw new DOMException("aborted", "AbortError");
  const normalizedKind = (kind || "qemu").toLowerCase();
  if (!["qemu", "container", "android"].includes(normalizedKind) || (name?.length ?? 0) > 128) {
    throw new ApiError(400, "環境の名前と種類を確認してください");
  }
  const targetKind = normalizedKind as CreationKind;
  const retry = vmCreationRetry(name || null, targetKind);
  const controller = new AbortController();
  const cancel = () => controller.abort();
  signal?.addEventListener("abort", cancel, { once: true });
  if (signal?.aborted) controller.abort();
  const timer = setTimeout(cancel, 600000);
  try {
    let job = creationResponse(await creationRequest("/vm-creations", {
      ...post({ name: retry.name, kind: retry.kind }),
      headers: { "Content-Type": "application/json", "Idempotency-Key": retry.key },
    }, controller.signal), targetKind);
    const jobId = job.id;
    const vmId = job.vm_id;
    while (job.state === "queued" || job.state === "running") {
      await waitForCreation(controller.signal);
      job = creationResponse(await creationRequest(`/vm-creations/${encodeURIComponent(jobId)}`, {
        cache: "no-store",
      }, controller.signal), targetKind);
      if (job.id !== jobId || job.vm_id !== vmId) throw new Error("別の作成要求の応答を受け取りました");
    }
    if (controller.signal.aborted) throw new DOMException("aborted", "AbortError");
    forgetVmCreation(retry.key);
    if (job.state === "failed") throw new ApiError(job.error_status!, job.error!);
    return job.result!;
  } catch (error) {
    if (signal?.aborted) throw new DOMException("aborted", "AbortError");
    if (error instanceof ApiError) throw error;
    throw new Error("作成結果を確認できません。同じ内容でもう一度操作してください", { cause: error });
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
  }
}

export async function deleteVm(vmId: string): Promise<{ status: string }> {
  return parseAcknowledgement(await request<unknown>(`/vms/${encodeURIComponent(vmId)}`, { method: "DELETE" }), "deleted");
}

export async function stopVm(vmId: string): Promise<VmInfo> {
  return parseVmInfo(await request<unknown>(`/vms/${encodeURIComponent(vmId)}/stop`, post()), vmId);
}

export async function startVm(vmId: string): Promise<VmInfo> {
  return parseVmInfo(await request<unknown>(`/vms/${encodeURIComponent(vmId)}/start`, post()), vmId);
}

export async function getVmStatus(signal?: AbortSignal): Promise<VmStatus> {
  return parseVmStatus(await request<unknown>("/vm/status", signal ? { signal } : undefined));
}

export async function restartVm(): Promise<VmStatus> {
  return parseVmStatus(await request<unknown>("/vm/restart", post()));
}

export async function getTaskHistory(limit = 20): Promise<TaskHistoryItem[]> {
  return parseTaskHistory(await request<unknown>(`/tasks?limit=${limit}`));
}

export async function getTaskDetail(taskId: string): Promise<TaskDetail> {
  return parseTaskDetail(await request<unknown>(`/tasks/${encodeURIComponent(taskId)}`), taskId);
}

export async function deleteTask(taskId: string): Promise<{ status: string }> {
  return parseAcknowledgement(await request<unknown>(`/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" }), "deleted");
}

export async function getSchedules(signal: AbortSignal) {
  return parseSchedules(await request<unknown>("/schedules", { signal }));
}

export async function addSchedule(vmId: string | null, instruction: string, runAt: number, interval: number, requestId: string, vmKind: ScheduleEnvironmentKind | null = null) {
  const payload = { vm_id: vmId, vm_kind: vmKind, instruction, run_at: runAt, interval, request_id: requestId };
  return parseSchedule(await request<unknown>("/schedules", post(payload)), requestId);
}

export async function deleteSchedule(id: string) {
  return parseAcknowledgement(await request<unknown>(`/schedules/${encodeURIComponent(id)}`, { method: "DELETE" }), "deleted");
}
