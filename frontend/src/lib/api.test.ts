import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  createTask,
  getCurrentTask,
  controlTask,
  getVmStatus,
  restartVm,
  getTaskHistory,
  getTaskDetail,
  deleteTask,
  getVms,
  createVm,
  deleteVm,
  getWsUrl,
  getVncWsUrl,
} from "@/lib/api";

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
          body: JSON.stringify({ instruction: "テスト指示", vm_id: null, allow_vm_restart: false }),
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

    it("uses the given port", () => {
      expect(getVncWsUrl(6090)).toContain(":6090");
    });
  });

  describe("getVmStatus", () => {
    it("returns VM status", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: true,
        json: async () => ({ running: true, status: "running", health: "healthy", name: "vm-1" }),
      } as Response);

      const result = await getVmStatus();
      expect(result.running).toBe(true);
      expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/vm/status"));
    });

    it("throws on HTTP error", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: false,
        status: 503,
      } as Response);

      await expect(getVmStatus()).rejects.toThrow("HTTP 503");
    });
  });

  describe("restartVm", () => {
    it("POSTs /vm/restart", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: true,
        json: async () => ({ running: true, status: "restarting", health: null, name: "vm-1" }),
      } as Response);

      const result = await restartVm();
      expect(result.status).toBe("restarting");
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining("/vm/restart"),
        expect.objectContaining({ method: "POST" })
      );
    });
  });

  describe("getTaskHistory", () => {
    it("returns history list", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: true,
        json: async () => [
          {
            id: "a1",
            instruction: "テスト",
            state: "completed",
            success: true,
            action_count: 2,
            success_count: 2,
            failure_count: 0,
            updated_at: 1700000000,
          },
        ],
      } as Response);

      const result = await getTaskHistory();
      expect(result).toHaveLength(1);
      expect(result[0].id).toBe("a1");
      expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/tasks"));
    });
  });

  describe("getTaskDetail", () => {
    it("returns task detail with actions", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          id: "a1",
          instruction: "テスト",
          state: "completed",
          success: true,
          action_count: 1,
          success_count: 1,
          failure_count: 0,
          updated_at: 1700000000,
          actions: [
            {
              action_type: "left_click",
              params: { x: 1, y: 2 },
              description: "左クリック",
              success: true,
              error_message: "",
              duration_ms: 10,
              at: 1700000000,
            },
          ],
          subtasks: [],
          goal: {},
          created_at: 1700000000,
        }),
      } as Response);

      const result = await getTaskDetail("a1");
      expect(result.actions).toHaveLength(1);
      expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/tasks/a1"));
    });

    it("throws on 404", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: false,
        status: 404,
      } as Response);

      await expect(getTaskDetail("nope")).rejects.toThrow("HTTP 404");
    });
  });

  describe("deleteTask", () => {
    it("DELETEs /tasks/{id}", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: true,
        json: async () => ({ status: "deleted" }),
      } as Response);

      const result = await deleteTask("a1");
      expect(result.status).toBe("deleted");
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining("/tasks/a1"),
        expect.objectContaining({ method: "DELETE" })
      );
    });
  });

  describe("getVms", () => {
    it("returns vm list", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: true,
        json: async () => [
          { id: "vm", name: "vm", status: "running", health: "healthy", vnc_port: 5900, ws_port: 6080, vnc_host: "vm", managed: false },
        ],
      } as Response);

      const result = await getVms();
      expect(result).toHaveLength(1);
      expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/vms"));
    });
  });

  describe("createVm/deleteVm", () => {
    it("POSTs /vms and DELETEs /vms/{id}", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: "vm-x", name: "x", status: "creating", health: null, vnc_port: 5910, ws_port: 6090, vnc_host: "h", managed: true }),
      } as Response);
      const created = await createVm("x");
      expect(created.id).toBe("vm-x");
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining("/vms"),
        expect.objectContaining({ method: "POST" })
      );

      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: true,
        json: async () => ({ status: "deleted" }),
      } as Response);
      const deleted = await deleteVm("vm-x");
      expect(deleted.status).toBe("deleted");
    });
  });
});
