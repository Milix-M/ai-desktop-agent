import { afterEach, beforeEach, expect, test, vi } from "vitest";

const response = (value: unknown) => ({ ok: true, status: 200, json: async () => value }) as Response;
beforeEach(() => { vi.resetModules(); sessionStorage.clear(); localStorage.clear(); });
afterEach(() => vi.restoreAllMocks());

test.each([null, [], {}, "authenticated", { authenticated: false }, { authenticated: "true" }].map(value => [value] as const))(
  "login requires affirmative authentication before opening a workspace: %j", async value => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response(value));
    const { login } = await import("./api");
    await expect(login("operator-private-key")).rejects.toThrow("認証の応答が不正です");
  },
);

test.each(["logout", "deleteTask", "deleteVm", "pause", "resume", "stop"] as const)(
  "%s cannot treat an absent, malformed or contradictory acknowledgement as success", async operation => {
    const fetch = vi.spyOn(globalThis, "fetch");
    const api = await import("./api");
    const run = () => operation === "logout" ? api.logout()
      : operation === "deleteTask" ? api.deleteTask("saved-task")
      : operation === "deleteVm" ? api.deleteVm("desktop")
      : api.controlTask(operation, "desktop", "observed-task");
    for (const value of [null, {}, { status: false }, { status: "no_session" }]) {
      fetch.mockResolvedValueOnce(response(value));
      await expect(run()).rejects.toThrow("操作の応答が不正です");
    }
  },
);

test("valid mutation acknowledgements retain extension fields and the actual action status", async () => {
  const fetch = vi.spyOn(globalThis, "fetch");
  const api = await import("./api");
  fetch.mockResolvedValueOnce(response({ authenticated: true, extension: "retained" }));
  expect(await api.login("operator-key")).toEqual({ authenticated: true, extension: "retained" });
  for (const [operation, status] of [
    [() => api.logout(), "logged_out"], [() => api.deleteTask("saved"), "deleted"],
    [() => api.deleteVm("desktop"), "deleted"],
    [() => api.controlTask("pause", "desktop", "observed"), "paused"],
    [() => api.controlTask("resume", "desktop", "observed"), "resumed"],
    [() => api.controlTask("stop", "desktop", "observed"), "stopped"],
  ] as const) {
    fetch.mockResolvedValueOnce(response({ status, extension: "retained" }));
    expect(await operation()).toEqual({ status, extension: "retained" });
  }
});
