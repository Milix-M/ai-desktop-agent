import type { PendingSubmission } from "./types";

const STORAGE_KEY = "ai-desktop-agent-task-submissions";
type Receipt = { fingerprint: string; requestId: string };
let memory: Receipt[] = [];
let storageBroken = false;
let generation = 0;

function pending(): Receipt[] {
  if (!storageBroken) {
    try {
      const value: unknown = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? "[]");
      if (!Array.isArray(value) || value.some(item => !item || typeof item !== "object" ||
        typeof item.fingerprint !== "string" || typeof item.requestId !== "string" ||
        !/^[a-f0-9]{64}$/.test(item.fingerprint) || !/^[a-f0-9]{32}$/.test(item.requestId))) {
        throw new Error("Invalid pending submissions");
      }
      memory = value.map(({ fingerprint, requestId }) => ({ fingerprint, requestId }));
    } catch { storageBroken = true; }
  }
  return memory;
}

function save(receipts: Receipt[]) {
  memory = receipts;
  try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(receipts)); storageBroken = false; }
  catch { storageBroken = true; }
}

export async function taskSubmissionRetry(attempt: PendingSubmission, forceNew = false): Promise<string> {
  // Persist only a digest and an opaque retry key; instructions stay out of browser storage.
  // Secure contexts (HTTPS and localhost) support Web Crypto. The component's in-memory
  // submission still preserves retries if hashing or session storage is unavailable.
  let fingerprint: string;
  const started = generation;
  try {
    const payload = JSON.stringify([attempt.instruction, attempt.vmId, attempt.allowVmRestart, ...(attempt.workspace ? [attempt.workspace] : [])]);
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(payload));
    fingerprint = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
  } catch {
    if (started !== generation) throw new DOMException("ログアウトしました", "AbortError");
    return attempt.requestId;
  }
  if (started !== generation) throw new DOMException("ログアウトしました", "AbortError");
  const receipts = pending();
  const existing = receipts.find(item => item.fingerprint === fingerprint);
  if (existing && !forceNew) return existing.requestId;
  save([...receipts.filter(item => item.fingerprint !== fingerprint), { fingerprint, requestId: attempt.requestId }]);
  return attempt.requestId;
}

export function forgetTaskSubmission(requestId: string) {
  save(pending().filter(item => item.requestId !== requestId));
}

export function clearTaskSubmissions() {
  generation++;
  save([]);
}
