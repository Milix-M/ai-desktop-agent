import { beforeEach, afterEach, expect, test, vi } from "vitest";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
const response = (body: unknown, status = 200) => ({
  ok: status >= 200 && status < 300, status, json: async () => body,
} as Response);

beforeEach(() => { vi.resetModules(); localStorage.clear(); });
afterEach(() => vi.restoreAllMocks());

test("a late 401 from before a successful login cannot lock the new session", async () => {
  const old = deferred<Response>();
  const fetchMock = vi.spyOn(globalThis, "fetch").mockReturnValueOnce(old.promise)
    .mockResolvedValueOnce(response({ authenticated: true }));
  const dispatch = vi.spyOn(window, "dispatchEvent");
  const api = await import("./api");
  const pending = expect(api.getCurrentTask()).rejects.toMatchObject({ status: 401 });
  await api.login("operator-key");
  old.resolve(response({ detail: "old session expired" }, 401));
  await pending;
  expect(dispatch).not.toHaveBeenCalled();
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

test("a 401 is checked against the current cookie before locking the screen", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(response({}, 401))
    .mockResolvedValueOnce(response({ authenticated: true }));
  const dispatch = vi.spyOn(window, "dispatchEvent");
  const api = await import("./api");
  await expect(api.getCurrentTask()).rejects.toMatchObject({ status: 401 });
  expect(dispatch).not.toHaveBeenCalled();
  expect(fetch).toHaveBeenLastCalledWith(expect.stringContaining("/auth/status"), expect.any(Object));
});

test("an expiry check already in flight cannot lock a newly authenticated session", async () => {
  const check = deferred<Response>();
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async url => {
    if (String(url).endsWith("/auth/status")) return check.promise;
    if (String(url).endsWith("/auth/login")) return response({ authenticated: true });
    return response({}, 401);
  });
  const dispatch = vi.spyOn(window, "dispatchEvent");
  const api = await import("./api");
  const pending = expect(api.getCurrentTask()).rejects.toMatchObject({ status: 401 });
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/auth/status"), expect.any(Object)));
  await api.login("operator-key");
  check.resolve(response({ authenticated: false }));
  await pending;
  expect(dispatch).not.toHaveBeenCalled();
});

test("simultaneous 401 responses share one current-session check and one lock event", async () => {
  const check = deferred<Response>();
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async url => {
    if (String(url).endsWith("/auth/status")) return check.promise;
    return response({}, 401);
  });
  const dispatch = vi.spyOn(window, "dispatchEvent");
  const api = await import("./api");
  const first = expect(api.getCurrentTask()).rejects.toMatchObject({ status: 401 });
  const second = expect(api.getVms()).rejects.toMatchObject({ status: 401 });
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  check.resolve(response({ authenticated: false }));
  await Promise.all([first, second]);
  expect(dispatch).toHaveBeenCalledTimes(1);
  expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/auth/status"))).toHaveLength(1);
});

test("an unavailable auth check cannot claim that the session has expired", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(response({}, 401)).mockRejectedValueOnce(new TypeError("offline"));
  const dispatch = vi.spyOn(window, "dispatchEvent");
  const api = await import("./api");
  await expect(api.getCurrentTask()).rejects.toMatchObject({ status: 401 });
  expect(dispatch).not.toHaveBeenCalled();
});

test("auth confirmation has a five-second deadline and releases its pending timer", async () => {
  vi.useFakeTimers();
  try {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(response({}, 401)).mockImplementationOnce((_url, init) =>
      new Promise((_, reject) => init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")))));
    const dispatch = vi.spyOn(window, "dispatchEvent");
    const api = await import("./api");
    const failed = expect(api.getCurrentTask()).rejects.toMatchObject({ status: 401 });
    await vi.advanceTimersByTimeAsync(5000);
    await failed;
    expect(dispatch).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  } finally { vi.useRealTimers(); }
});

test("auth-changing requests stay ordered so logout cannot overtake login", async () => {
  const first = deferred<Response>();
  const fetchMock = vi.spyOn(globalThis, "fetch").mockReturnValueOnce(first.promise)
    .mockResolvedValueOnce(response({ status: "logged_out" }));
  const api = await import("./api");
  const loggedIn = api.login("operator-private-key");
  const loggedOut = api.logout();
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  expect(String(fetchMock.mock.calls[0][0])).toContain("/auth/login");
  first.resolve(response({ authenticated: true }));
  await Promise.all([loggedIn, loggedOut]);
  expect(String(fetchMock.mock.calls[1][0])).toContain("/auth/logout");
  expect(JSON.stringify(localStorage)).not.toContain("operator-private-key");
});

test("a rejected login cannot strand a queued logout or suppress a later real expiry", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(response({ detail: "wrong key" }, 401))
    .mockResolvedValueOnce(response({ status: "logged_out" }))
    .mockResolvedValueOnce(response({}, 401)).mockResolvedValueOnce(response({ authenticated: false }));
  const dispatch = vi.spyOn(window, "dispatchEvent");
  const api = await import("./api");
  const rejected = expect(api.login("incorrect")).rejects.toMatchObject({ status: 401 });
  const loggedOut = api.logout();
  await Promise.all([rejected, loggedOut]);
  expect(dispatch).not.toHaveBeenCalled();
  await expect(api.getCurrentTask()).rejects.toMatchObject({ status: 401 });
  expect(dispatch).toHaveBeenCalledTimes(1);
});

test("a throttled login preserves its retry message and releases the authentication queue", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(response({ detail: "認証の失敗が続いています。60秒後にもう一度操作してください" }, 429))
    .mockResolvedValueOnce(response({ authenticated: true }));
  const dispatch = vi.spyOn(window, "dispatchEvent");
  const api = await import("./api");
  await expect(api.login("operator-key")).rejects.toMatchObject({ status: 429, message: expect.stringContaining("60秒後") });
  expect(dispatch).not.toHaveBeenCalled();
  await expect(api.login("operator-key")).resolves.toEqual({ authenticated: true });
  expect(fetch).toHaveBeenCalledTimes(2);
});

test("a late 401 cannot lock a login completed in another tab", async () => {
  const old = deferred<Response>();
  const fetchMock = vi.spyOn(globalThis, "fetch").mockReturnValueOnce(old.promise);
  const dispatch = vi.spyOn(window, "dispatchEvent");
  const api = await import("./api");
  const pending = expect(api.getCurrentTask()).rejects.toMatchObject({ status: 401 });
  localStorage.setItem("ai-desktop-agent-auth-change", "another-tab-finished-login");
  old.resolve(response({}, 401));
  await pending;
  expect(dispatch).not.toHaveBeenCalled();
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test.each(["false", "error"])("an obsolete auth %s from before another tab's login is checked again", async failure => {
  const old = deferred<Response>();
  const fetchMock = vi.spyOn(globalThis, "fetch").mockReturnValueOnce(old.promise)
    .mockResolvedValueOnce(response({ authenticated: true }));
  const api = await import("./api");
  const result = api.getAuthStatus();
  localStorage.setItem("ai-desktop-agent-auth-change", "another-tab-finished-login");
  old.resolve(response(failure === "false" ? { authenticated: false } : {}, failure === "false" ? 200 : 503));
  expect(await result).toEqual({ authenticated: true });
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

test("same-tab auth guards still work when browser storage is unavailable", async () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new DOMException("blocked", "SecurityError"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("blocked", "SecurityError"); });
  const old = deferred<Response>();
  vi.spyOn(globalThis, "fetch").mockReturnValueOnce(old.promise).mockResolvedValueOnce(response({ authenticated: true }));
  const dispatch = vi.spyOn(window, "dispatchEvent");
  const api = await import("./api");
  const pending = expect(api.getCurrentTask()).rejects.toMatchObject({ status: 401 });
  await api.login("operator-key");
  old.resolve(response({}, 401));
  await pending;
  expect(dispatch).not.toHaveBeenCalled();
});

test.each(["login", "logout"])("a stalled %s cannot block the next authentication change forever", async action => {
  vi.useFakeTimers();
  let release!: () => void;
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementationOnce((_url, init) => new Promise((resolve, reject) => {
    release = () => resolve(response({ authenticated: true, status: "logged_out" }));
    init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  })).mockResolvedValueOnce(response({ authenticated: true, status: "logged_out" }));
  const api = await import("./api");
  const first = (action === "login" ? api.login("operator-key") : api.logout()).then(() => null, error => error);
  const next = action === "login" ? api.logout() : api.login("operator-key");
  try {
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(await first).toBeInstanceOf(Error);
    await next;
    // jsdom queues storage events inside the timeout callback; fake timers defer them by 1 ms.
    await vi.advanceTimersByTimeAsync(1);
    expect(vi.getTimerCount()).toBe(0);
  } finally {
    release?.();
    await Promise.allSettled([first, next]);
    vi.useRealTimers();
  }
});
