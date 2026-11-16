import { expect, it, vi } from "vitest";
import { controlTask } from "./api";

it("sends the selected VM with control requests", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
    ok: true, json: async () => ({ status: "stopped" }),
  } as Response);
  try {
    await controlTask("stop", "vm A");
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/tasks/current/stop?vm_id=vm%20A"), { method: "POST", credentials: "include" }
    );
  } finally { fetchMock.mockRestore(); }
});
