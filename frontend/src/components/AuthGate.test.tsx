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
