import { afterEach, beforeEach, expect, test, vi } from "vitest";

const response = (authenticated: boolean) => ({ ok: true, status: 200,
  json: async () => ({ authenticated }) }) as Response;

beforeEach(() => { vi.resetModules(); localStorage.clear(); });
afterEach(() => { vi.restoreAllMocks(); });

test("finishing an old authentication check cannot discard the current login's shared check", async () => {
  let oldReply!: (value: Response) => void;
  let currentReply!: (value: Response) => void;
  const previous = new Promise<Response>(resolve => { oldReply = resolve; });
  const current = new Promise<Response>(resolve => { currentReply = resolve; });
  let checks = 0;
  const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async url => {
    const path = new URL(String(url)).pathname;
    if (path === "/auth/status") {
      checks++;
      if (checks === 1) return previous;
      if (checks === 2) return current;
      if (checks === 3) return response(true);
    }
    if (path === "/auth/login") return response(true);
    throw new Error(`unexpected request: ${path}`);
  });
  const dispatch = vi.spyOn(window, "dispatchEvent");
  const api = await import("./api");
  const pending: Promise<void>[] = [];
  try {
    const first = api.confirmAuthentication();
    pending.push(first);
    expect(checks).toBe(1);
    await api.login("test-only-login-key");
    const second = api.confirmAuthentication();
    pending.push(second);
    expect(checks).toBe(2);
    oldReply(response(false));
    await first;
    // The old request revalidates once after the authentication version changes.
    expect(checks).toBe(3);
    const third = api.confirmAuthentication();
    pending.push(third);
    expect(checks).toBe(3);
    expect(fetch).toHaveBeenCalledTimes(4);
    currentReply(response(true));
    await Promise.all([second, third]);
    expect(dispatch).not.toHaveBeenCalledWith(expect.objectContaining({ type: "auth-required" }));
  } finally {
    oldReply(response(true)); currentReply(response(true));
    await Promise.allSettled(pending);
  }
});
