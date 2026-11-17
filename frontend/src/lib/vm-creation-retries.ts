export type CreationKind = "qemu" | "container" | "android";
export interface VmCreationRetry { key: string; name: string | null; kind: CreationKind }

const STORAGE_KEY = "ai-desktop-agent-vm-creations";
let memory: VmCreationRetry[] = [];
let storageBroken = false;

export function pendingVmCreations(): VmCreationRetry[] {
  if (!storageBroken) {
    try {
      const parsed: unknown = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? "[]");
      if (!Array.isArray(parsed) || parsed.some(item => !item || typeof item !== "object" ||
        typeof item.key !== "string" ||
        !/^[A-Za-z0-9_-]{16,128}$/.test(item.key) ||
        (item.name !== null && (typeof item.name !== "string" || item.name.length > 128)) ||
        !["qemu", "container", "android"].includes(item.kind))) {
        throw new Error("Invalid pending creations");
      }
      memory = parsed;
    } catch { storageBroken = true; }
  }
  return memory.map(item => ({ ...item }));
}

function save(items: VmCreationRetry[]) {
  memory = items;
  try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(items)); storageBroken = false; }
  catch { storageBroken = true; }
}

export function vmCreationRetry(name: string | null, kind: CreationKind): VmCreationRetry {
  const pending = pendingVmCreations();
  const existing = pending.find(item => item.name === name && item.kind === kind);
  if (existing) return existing;
  const key = Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, "0")).join("");
  const created = { key, name, kind };
  save([...pending, created]);
  return created;
}

export function forgetVmCreation(key: string) {
  save(pendingVmCreations().filter(item => item.key !== key));
}
