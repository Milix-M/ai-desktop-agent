import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  createTask,
  ApiError,
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
  stopVm,
  startVm,
  getWsUrl,
  getVncWsUrl,
} from "@/lib/api";

describe("API client", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    sessionStorage.clear();
  });

  describe("createTask", () => {
    it("sends the retry key and preserves definitive HTTP status", async () => {
      const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: false, status: 410, json: async () => ({ detail: "受付済み・履歴削除済み" }),
      } as Response);
      await expect(createTask("報告書", "desktop", false, "retry-0000000000000001"))
        .rejects.toMatchObject({ status: 410 });
      expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/tasks"), expect.objectContaining({
        headers: { "Content-Type": "application/json", "Idempotency-Key": "retry-0000000000000001" },
        signal: expect.any(AbortSignal),
      }));
    });

    it("bounds an uncertain response and retains a useful retry message", async () => {
      vi.useFakeTimers();
      try {
        vi.spyOn(globalThis, "fetch").mockImplementation((_url, init) => new Promise((_, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        }));
        const result = expect(createTask("報告書", "desktop", false, "retry-0000000000000001"))
          .rejects.toThrow("受付結果を確認できません");
        await vi.advanceTimersByTimeAsync(30000);
        await result;
        expect(vi.getTimerCount()).toBe(0);
      } finally { vi.useRealTimers(); }
    });

    it("treats a broken successful response as uncertain, rather than an HTTP rejection", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: true, json: async () => { throw new SyntaxError("truncated JSON"); },
      } as unknown as Response);
      try { await createTask("報告書", "desktop", false, "retry-0000000000000001"); }
      catch (error) {
        expect(error).not.toBeInstanceOf(ApiError);
        expect((error as Error).message).toContain("同じ内容で再送してください");
        return;
      }
      throw new Error("expected an uncertain response failure");
    });

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
    it("binds a stop to the task that was visible when it was sent", async () => {
      const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: true, json: async () => ({ status: "stopped" }),
      } as Response);
      await controlTask("stop", "vm A", "observed-task");
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/tasks/current/stop?vm_id=vm%20A&session_id=observed-task"),
        expect.objectContaining({ method: "POST" }),
      );
    });

    it("binds consent to the current manual request and VM", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({ ok: true, json: async () => ({ status: "resumed" }) } as Response);
      await controlTask("resume", "desktop", "observed-task", "current-request");
      expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/tasks/current/resume?vm_id=desktop&session_id=observed-task"), expect.objectContaining({
        body: JSON.stringify({ manual_input_id: "current-request", allow_screen_share: true }),
      }));
    });

    it.each(["pause", "resume", "stop"] as const)(
      "POSTs /tasks/current/%s",
      async (action) => {
        vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
          ok: true,
          json: async () => ({ status: action + "d" }),
        } as Response);

        const result = await controlTask(action, "desktop", "observed-task");
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
    it("returns an authenticated backend path for the default VM", () => {
      const url = getVncWsUrl();
      expect(url).toContain(":8081/vnc/vm");
    });

    it("encodes the environment ID", () => {
      expect(getVncWsUrl("desk/one")).toContain(":8081/vnc/desk%2Fone");
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
      expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/vm/status"), expect.objectContaining({ credentials: "include" }));
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
      expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/tasks"), expect.objectContaining({ credentials: "include" }));
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
      expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/tasks/a1"), expect.objectContaining({ credentials: "include" }));
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
      expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/vms"), expect.objectContaining({ credentials: "include" }));
    });
  });

  describe("createVm/deleteVm", () => {
    it("POSTs a keyed creation and DELETEs /vms/{id}", async () => {
      const vm = { id: "vm-x", kind: "qemu", name: "x", status: "creating", health: null, vnc_port: 0, ws_port: 0, vnc_host: "h", managed: true };
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: "creation-" + "a".repeat(64), vm_id: vm.id, state: "succeeded", result: vm }),
      } as Response);
      const created = await createVm("x");
      expect(created.id).toBe("vm-x");
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining("/vm-creations"),
        expect.objectContaining({ method: "POST", headers: expect.objectContaining({ "Idempotency-Key": expect.any(String) }) })
      );

      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: true,
        json: async () => ({ status: "deleted" }),
      } as Response);
      const deleted = await deleteVm("vm-x");
      expect(deleted.status).toBe("deleted");
    });
  });

  describe("stopVm/startVm", () => {
    it("POSTs /vms/{id}/stop", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: "vm", name: "vm", status: "exited", health: null, vnc_port: 5900, ws_port: 6080, vnc_host: "h", managed: false }),
      } as Response);
      const result = await stopVm("vm");
      expect(result.status).toBe("exited");
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining("/vms/vm/stop"),
        expect.objectContaining({ method: "POST" })
      );
    });

    it("POSTs /vms/{id}/start", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: "desktop", name: "desktop", status: "running", health: null, vnc_port: 5901, ws_port: 6081, vnc_host: "h", managed: false }),
      } as Response);
      const result = await startVm("desktop");
      expect(result.status).toBe("running");
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining("/vms/desktop/start"),
        expect.objectContaining({ method: "POST" })
      );
    });

    it("throws on HTTP error", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: false,
        status: 404,
      } as Response);
      await expect(stopVm("nope")).rejects.toThrow("HTTP 404");
    });
  });
});

it("preserves actionable backend errors", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
    ok: false, status: 409,
    json: async () => ({ detail: "KVMを利用できません。コンテナ環境を選んでください" }),
  } as Response);
  await expect(createVm("作業環境", "qemu")).rejects.toThrow("コンテナ環境を選んでください");
});
