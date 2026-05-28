import type { TaskStatus } from "./types";

const BACKEND_URL =
  typeof window !== "undefined"
    ? `${window.location.protocol}//${window.location.hostname}:8080`
    : "http://localhost:8080";

export async function createTask(
  instruction: string
): Promise<TaskStatus> {
  const resp = await fetch(`${BACKEND_URL}/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instruction }),
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
  if (typeof window === "undefined") return "ws://localhost:8080/ws";
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.hostname}:8080/ws`;
}

export function getVncWsUrl(): string {
  if (typeof window === "undefined") return "ws://localhost:6080";
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.hostname}:6080`;
}
