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
