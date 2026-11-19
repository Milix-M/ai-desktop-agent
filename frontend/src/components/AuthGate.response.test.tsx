import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import AuthGate from "./AuthGate";

let authenticated: boolean;
let loginResponse: unknown;
let logoutResponse: unknown;
const response = (value: unknown) => ({ ok: true, status: 200, json: async () => value }) as Response;
beforeEach(() => {
  sessionStorage.clear(); localStorage.clear(); authenticated = false;
  loginResponse = { authenticated: true }; logoutResponse = { status: "logged_out" };
  vi.spyOn(globalThis, "fetch").mockImplementation(async url => {
    const path = new URL(String(url)).pathname;
    if (path === "/auth/status") return response({ authenticated });
    if (path === "/auth/login") return response(loginResponse);
    if (path === "/auth/logout") return response(logoutResponse);
    throw new Error(`unexpected request: ${path}`);
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

test.each([null, { authenticated: false }, { authenticated: "true" }])(
  "an invalid login keeps the form and key available for a deliberate retry: %j", async value => {
    loginResponse = value;
    render(<AuthGate>{actions => <>{actions}<div>Desktop workspace</div></>}</AuthGate>);
    fireEvent.change(await screen.findByLabelText("接続キー"), { target: { value: "operator-private-key" } });
    fireEvent.click(screen.getByRole("button", { name: "ログイン" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("認証の応答が不正です");
    expect(screen.queryByText("Desktop workspace")).not.toBeInTheDocument();
    expect(screen.getByLabelText("接続キー")).toHaveValue("operator-private-key");
    expect(JSON.stringify(localStorage) + JSON.stringify(sessionStorage)).not.toContain("operator-private-key");
    loginResponse = { authenticated: true };
    fireEvent.click(screen.getByRole("button", { name: "ログイン" }));
    expect(await screen.findByText("Desktop workspace")).toBeVisible();
  },
);

test("an invalid logout confirmation preserves the workspace and invokes cleanup only after a valid retry", async () => {
  authenticated = true;
  logoutResponse = null;
  const onLogout = vi.fn();
  render(<AuthGate onLogout={onLogout}>{actions => <>{actions}<div>Desktop workspace</div></>}</AuthGate>);
  fireEvent.click(await screen.findByRole("button", { name: "ログアウト" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("ログアウトできません");
  expect(screen.getByText("Desktop workspace")).toBeVisible();
  expect(onLogout).not.toHaveBeenCalled();
  logoutResponse = { status: "logged_out" };
  fireEvent.click(screen.getByRole("button", { name: "ログアウト" }));
  await screen.findByLabelText("接続キー");
  expect(onLogout).toHaveBeenCalledOnce();
});
