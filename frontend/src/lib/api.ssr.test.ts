// @vitest-environment node

import { afterEach, expect, test, vi } from "vitest";
import { ApiError, getTaskHistory, getVmStatus, getVncWsUrl, getWsUrl } from "./api";

afterEach(() => vi.restoreAllMocks());

test("API modules can load and construct transport URLs without browser globals", async () => {
  expect(getWsUrl()).toBe("ws://localhost:8081/ws");
  expect(getVncWsUrl("vm /旧環境")).toBe("ws://localhost:8081/vnc/vm%20%2F%E6%97%A7%E7%92%B0%E5%A2%83");
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({ ok: true, json: async () => [] } as unknown as Response);
  expect(await getTaskHistory()).toEqual([]);
  expect(fetch).toHaveBeenCalledExactlyOnceWith("http://localhost:8081/tasks?limit=20", { credentials: "include", signal: expect.any(AbortSignal) });
});

test("a server-rendered unauthorized response remains an HTTP error without browser auth events", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({ ok: false, status: 401,
    json: async () => ({ detail: "authentication required" }) } as Response);
  await expect(getVmStatus()).rejects.toBeInstanceOf(ApiError);
  expect(fetch).toHaveBeenCalledOnce();
});
