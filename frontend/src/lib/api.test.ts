import { describe, it, expect, beforeEach, vi } from "vitest";
import { createTask, getCurrentTask, controlTask, getWsUrl, getVncWsUrl } from "@/lib/api";

describe("API client", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  describe("createTask", () => {
    it("sends POST with instruction and returns TaskStatus", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          session_id: "abc",
          state: "executing",
          is_running: true,
          action_count: 0,
          success_count: 0,
          failure_count: 0,
        }),
      } as Response);

      const result = await createTask("テスト指示");

      expect(result.state).toBe("executing");
      expect(result.session_id).toBe("abc");
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining("/tasks"),
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ instruction: "テスト指示" }),
        })
      );
    });

    it("throws on HTTP error", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: false,
        status: 500,
      } as Response);

      await expect(createTask("err")).rejects.toThrow("HTTP 500");
    });
  });

  describe("getCurrentTask", () => {
    it("returns idle status when no session", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          session_id: null,
          state: "idle",
          is_running: false,
          action_count: 0,
          success_count: 0,
          failure_count: 0,
        }),
      } as Response);

      const result = await getCurrentTask();
      expect(result.state).toBe("idle");
    });
  });

  describe("controlTask", () => {
    it.each(["pause", "resume", "stop"] as const)(
      "POSTs /tasks/current/%s",
      async (action) => {
        vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
          ok: true,
          json: async () => ({ status: action + "d" }),
        } as Response);

        const result = await controlTask(action);
        expect(result.status).toBe(action + "d");
        expect(fetch).toHaveBeenCalledWith(
          expect.stringContaining(`/tasks/current/${action}`),
          expect.objectContaining({ method: "POST" })
        );
      }
    );
  });

  describe("getWsUrl", () => {
    it("returns ws URL with port 8081", () => {
      const url = getWsUrl();
      expect(url).toContain(":8081/ws");
    });
  });

  describe("getVncWsUrl", () => {
    it("returns ws URL with port 6080", () => {
      const url = getVncWsUrl();
      expect(url).toContain(":6080");
    });
  });
});
