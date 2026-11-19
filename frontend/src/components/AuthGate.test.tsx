import { render, screen, fireEvent, waitFor, cleanup, act } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import AuthGate from "./AuthGate";
import { getAuthStatus, login, logout } from "@/lib/api";

vi.mock("@/lib/api", () => ({ getAuthStatus: vi.fn(), login: vi.fn(), logout: vi.fn() }));
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(getAuthStatus).mockResolvedValue({ authenticated: false });
  vi.mocked(login).mockResolvedValue({ authenticated: true });
  vi.mocked(logout).mockResolvedValue({ status: "logged_out" });
});
afterEach(cleanup);

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function workspace(onLogout?: () => void) {
  return render(<AuthGate onLogout={onLogout}>{actions => <>{actions}<div>Desktop workspace</div></>}</AuthGate>);
}

test("a failed initial check explains the connection failure and permits a deliberate login", async () => {
  vi.mocked(getAuthStatus).mockRejectedValueOnce(new TypeError("network unavailable"));
  workspace();
  expect(await screen.findByRole("alert")).toHaveTextContent("サーバーに接続できません");
  const key = screen.getByLabelText("接続キー");
  fireEvent.submit(key.closest("form")!);
  expect(login).not.toHaveBeenCalled();
  fireEvent.change(key, { target: { value: "operator-key" } });
  fireEvent.click(screen.getByRole("button", { name: "ログイン" }));
  expect(await screen.findByText("Desktop workspace")).toBeVisible();
});

test.each(["resolved", "rejected"])("an initial check that finishes after unmount stays inactive: %s", async outcome => {
  const check = deferred<{ authenticated: boolean }>();
  vi.mocked(getAuthStatus).mockReturnValueOnce(check.promise);
  const { unmount } = workspace();
  const signal = vi.mocked(getAuthStatus).mock.calls[0][0];
  unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => { if (outcome === "resolved") check.resolve({ authenticated: true }); else check.reject(new Error("late error")); });
  expect(screen.queryByText("Desktop workspace")).not.toBeInTheDocument();
  expect(login).not.toHaveBeenCalled();
});

test("a rejected initial check cannot replace a newer authenticated workspace", async () => {
  const check = deferred<{ authenticated: boolean }>();
  vi.mocked(getAuthStatus).mockReturnValueOnce(check.promise);
  workspace();
  act(() => window.dispatchEvent(new Event("auth-required")));
  fireEvent.change(await screen.findByLabelText("接続キー"), { target: { value: "operator-key" } });
  fireEvent.click(screen.getByRole("button", { name: "ログイン" }));
  await screen.findByText("Desktop workspace");
  await act(async () => check.reject(new Error("retired check")));
  expect(screen.getByText("Desktop workspace")).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test.each(["resolved", "rejected"])("an expired in-flight login cannot unlock the workspace or replace the current message: %s", async outcome => {
  const pending = deferred<{ authenticated: boolean }>();
  vi.mocked(login).mockReturnValueOnce(pending.promise);
  workspace();
  fireEvent.change(await screen.findByLabelText("接続キー"), { target: { value: "operator-key" } });
  fireEvent.click(screen.getByRole("button", { name: "ログイン" }));
  act(() => window.dispatchEvent(new Event("auth-required")));
  await act(async () => { if (outcome === "resolved") pending.resolve({ authenticated: true }); else pending.reject(new Error("retired login error")); });
  expect(screen.getByRole("alert")).toHaveTextContent("ログインしてください");
  expect(screen.queryByText("Desktop workspace")).not.toBeInTheDocument();
  expect(screen.getByLabelText("接続キー")).toHaveValue("");
});

test.each(["resolved", "rejected"])("an unmounted login cannot affect its former workspace: %s", async outcome => {
  const pending = deferred<{ authenticated: boolean }>();
  vi.mocked(login).mockReturnValueOnce(pending.promise);
  const { unmount } = workspace();
  fireEvent.change(await screen.findByLabelText("接続キー"), { target: { value: "operator-key" } });
  fireEvent.click(screen.getByRole("button", { name: "ログイン" }));
  unmount();
  await act(async () => { if (outcome === "resolved") pending.resolve({ authenticated: true }); else pending.reject(new Error("late failure")); });
  expect(screen.queryByText("Desktop workspace")).not.toBeInTheDocument();
});

test("a non-Error login failure still explains the failure without unlocking the workspace", async () => {
  vi.mocked(login).mockRejectedValueOnce(null);
  workspace();
  fireEvent.change(await screen.findByLabelText("接続キー"), { target: { value: "operator-key" } });
  fireEvent.click(screen.getByRole("button", { name: "ログイン" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("ログインできません");
  expect(screen.queryByText("Desktop workspace")).not.toBeInTheDocument();
});

test("a failed logout preserves the active workspace and allows an explicit retry", async () => {
  vi.mocked(getAuthStatus).mockResolvedValueOnce({ authenticated: true });
  vi.mocked(logout).mockRejectedValueOnce(new Error("server unreachable"));
  const onLogout = vi.fn();
  workspace(onLogout);
  fireEvent.click(await screen.findByRole("button", { name: "ログアウト" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("ログアウトできません");
  expect(screen.getByText("Desktop workspace")).toBeVisible();
  expect(onLogout).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "ログアウト" }));
  await screen.findByLabelText("接続キー");
  expect(onLogout).toHaveBeenCalledOnce();
});

test.each(["resolved", "rejected"])("a logout finishing after expiry keeps the expired session locked: %s", async outcome => {
  vi.mocked(getAuthStatus).mockResolvedValueOnce({ authenticated: true });
  const pending = deferred<{ status: string }>();
  vi.mocked(logout).mockReturnValueOnce(pending.promise);
  workspace();
  const button = await screen.findByRole("button", { name: "ログアウト" });
  act(() => { button.click(); button.click(); });
  expect(logout).toHaveBeenCalledOnce();
  act(() => window.dispatchEvent(new Event("auth-required")));
  await act(async () => { if (outcome === "resolved") pending.resolve({ status: "logged_out" }); else pending.reject(new Error("old logout error")); });
  expect(screen.getByRole("alert")).toHaveTextContent("ログインしてください");
  expect(screen.queryByText("Desktop workspace")).not.toBeInTheDocument();
});

test.each(["resolved", "rejected"])("logout completion after unmount cannot clear a later workspace: %s", async outcome => {
  vi.mocked(getAuthStatus).mockResolvedValueOnce({ authenticated: true });
  const pending = deferred<{ status: string }>();
  vi.mocked(logout).mockReturnValueOnce(pending.promise);
  const onLogout = vi.fn();
  const { unmount } = workspace(onLogout);
  fireEvent.click(await screen.findByRole("button", { name: "ログアウト" }));
  unmount();
  await act(async () => { if (outcome === "resolved") pending.resolve({ status: "logged_out" }); else pending.reject(new Error("late error")); });
  expect(onLogout).not.toHaveBeenCalled();
});

test.each([false, true])("an old initial check (%s) cannot undo a later successful login", async (authenticated) => {
  let finish!: (value: { authenticated: boolean }) => void;
  vi.mocked(getAuthStatus).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  render(<AuthGate>{() => <div>Desktop workspace</div>}</AuthGate>);
  act(() => window.dispatchEvent(new Event("auth-required")));
  fireEvent.change(await screen.findByLabelText("接続キー"), { target: { value: "operator-key" } });
  fireEvent.click(screen.getByRole("button", { name: "ログイン" }));
  await screen.findByText("Desktop workspace");
  await act(async () => finish({ authenticated }));
  expect(screen.getByText("Desktop workspace")).toBeVisible();
});

test("an old successful check cannot reopen a session after an expiry notification", async () => {
  let finish!: (value: { authenticated: boolean }) => void;
  vi.mocked(getAuthStatus).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  render(<AuthGate>{() => <div>Desktop workspace</div>}</AuthGate>);
  act(() => window.dispatchEvent(new Event("auth-required")));
  await screen.findByLabelText("接続キー");
  await act(async () => finish({ authenticated: true }));
  expect(screen.queryByText("Desktop workspace")).not.toBeInTheDocument();
  expect(screen.getByLabelText("接続キー")).toBeVisible();
});

test("waits for authentication before mounting the desktop and keeps the key out of storage", async () => {
  render(<AuthGate>{(actions) => <>{actions}<div>Desktop workspace</div></>}</AuthGate>);
  expect(screen.queryByText("Desktop workspace")).not.toBeInTheDocument();
  const key = await screen.findByLabelText("接続キー");
  expect(key).toHaveAttribute("type", "password");
  fireEvent.change(key, { target: { value: "operator-private-access-key" } });
  fireEvent.click(screen.getByRole("button", { name: "ログイン" }));
  await screen.findByText("Desktop workspace");
  expect(login).toHaveBeenCalledWith("operator-private-access-key");
  expect(screen.queryByLabelText("接続キー")).not.toBeInTheDocument();
  expect(JSON.stringify(localStorage)).not.toContain("operator-private-access-key");
  expect(JSON.stringify(sessionStorage)).not.toContain("operator-private-access-key");
  fireEvent.click(screen.getByRole("button", { name: "ログアウト" }));
  await screen.findByLabelText("接続キー");
  expect(logout).toHaveBeenCalledOnce();
  expect(screen.queryByText("Desktop workspace")).not.toBeInTheDocument();
});

test("retains a useful login error and never mounts a rejected session", async () => {
  vi.mocked(login).mockRejectedValueOnce(new Error("接続キーが違います"));
  render(<AuthGate>{(actions) => <>{actions}<div>Desktop workspace</div></>}</AuthGate>);
  fireEvent.change(await screen.findByLabelText("接続キー"), { target: { value: "incorrect" } });
  fireEvent.click(screen.getByRole("button", { name: "ログイン" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("接続キーが違います");
  expect(screen.queryByText("Desktop workspace")).not.toBeInTheDocument();
});

test("a throttled login shows the retry delay and allows a later deliberate attempt", async () => {
  vi.mocked(login).mockRejectedValueOnce(new Error("認証の失敗が続いています。60秒後にもう一度操作してください (HTTP 429)"));
  render(<AuthGate>{() => <div>Desktop workspace</div>}</AuthGate>);
  fireEvent.change(await screen.findByLabelText("接続キー"), { target: { value: "operator-key" } });
  fireEvent.click(screen.getByRole("button", { name: "ログイン" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("60秒後");
  expect(screen.getByRole("button", { name: "ログイン" })).toBeEnabled();
  expect(login).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("Desktop workspace")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "ログイン" }));
  await screen.findByText("Desktop workspace");
  expect(login).toHaveBeenCalledTimes(2);
});

test("an expired session unmounts the workspace", async () => {
  vi.mocked(getAuthStatus).mockResolvedValueOnce({ authenticated: true });
  render(<AuthGate>{(actions) => <>{actions}<div>Desktop workspace</div></>}</AuthGate>);
  await screen.findByText("Desktop workspace");
  act(() => window.dispatchEvent(new Event("auth-required")));
  await screen.findByLabelText("接続キー");
  expect(screen.queryByText("Desktop workspace")).not.toBeInTheDocument();
});

test("submitting twice cannot create duplicate sessions", async () => {
  let finish!: (value: { authenticated: boolean }) => void;
  vi.mocked(login).mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
  render(<AuthGate>{(actions) => <>{actions}<div>Desktop workspace</div></>}</AuthGate>);
  fireEvent.change(await screen.findByLabelText("接続キー"), { target: { value: "operator-key" } });
  const button = screen.getByRole("button", { name: "ログイン" });
  fireEvent.submit(button.closest("form")!);
  fireEvent.submit(button.closest("form")!);
  expect(login).toHaveBeenCalledOnce();
  await act(async () => finish({ authenticated: true }));
  await waitFor(() => expect(screen.getByText("Desktop workspace")).toBeInTheDocument());
});
