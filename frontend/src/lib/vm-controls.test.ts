import { expect, it, vi } from "vitest";
import { controlTask } from "./api";

it("sends the selected VM and observed task with control requests", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
    ok: true, json: async () => ({ status: "stopped" }),
  } as Response);
  try {
    await controlTask("stop", "vm A", "observed-task");
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/tasks/current/stop?vm_id=vm%20A&session_id=observed-task"), expect.objectContaining({ method: "POST", credentials: "include", signal: expect.any(AbortSignal) })
    );
  } finally { fetchMock.mockRestore(); }
});
