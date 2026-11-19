import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { installBootstrapRecovery } from "./bootstrap-recovery";

const key = "ai-desktop-agent-bootstrap-retry";
let cleanups: Array<() => void>;
beforeEach(() => {
  vi.useFakeTimers(); sessionStorage.clear(); cleanups = [];
  document.body.innerHTML = '<main><p role="status">接続中…</p></main>';
});
afterEach(() => {
  cleanups.forEach(cleanup => cleanup());
  document.body.replaceChildren(); vi.restoreAllMocks(); vi.useRealTimers();
});
function install() {
  const reload = vi.fn();
  cleanups.push(installBootstrapRecovery(window, reload));
  return reload;
}
function fail(src = "/_next/static/chunks/app.js") {
  const script = document.createElement("script");
  script.src = src; document.body.append(script);
  script.dispatchEvent(new Event("error"));
}

test("the first application script failure retries once and coalesces simultaneous failures", () => {
  const reload = install();
  fail(); fail("/_next/static/chunks/runtime.js");
  expect(sessionStorage.getItem(key)).toBe("1");
  vi.advanceTimersByTime(999);
  expect(reload).not.toHaveBeenCalled();
  vi.advanceTimersByTime(1);
  expect(reload).toHaveBeenCalledOnce();
  vi.advanceTimersByTime(10000);
  expect(reload).toHaveBeenCalledOnce();
});

test.each(["1", "corrupt"])("an existing retry marker %s offers an explicit reload without looping", value => {
  sessionStorage.setItem(key, value);
  const reload = install(); fail();
  expect(document.querySelector('[role="alert"]')).toHaveTextContent("画面を読み込めませんでした");
  vi.advanceTimersByTime(10000);
  expect(reload).not.toHaveBeenCalled();
  const button = document.querySelector("button")!;
  button.click(); button.click();
  expect(reload).toHaveBeenCalledOnce();
  expect(sessionStorage.getItem(key)).toBeNull();
});

test.each(["getItem", "setItem"] as const)("unavailable storage during %s uses manual recovery", method => {
  vi.spyOn(Storage.prototype, method).mockImplementation(() => { throw new DOMException("blocked", "SecurityError"); });
  vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => { throw new DOMException("blocked", "SecurityError"); });
  const reload = install(); fail();
  expect(document.querySelector('[role="alert"]')).toBeVisible();
  vi.advanceTimersByTime(10000);
  expect(reload).not.toHaveBeenCalled();
  document.querySelector("button")!.click();
  expect(reload).toHaveBeenCalledOnce();
});

test("failure before the body is parsed waits for DOMContentLoaded before showing a retry", () => {
  sessionStorage.setItem(key, "1");
  vi.spyOn(document, "readyState", "get").mockReturnValue("loading");
  const reload = install(); fail();
  expect(document.querySelector('[role="alert"]')).toBeNull();
  document.dispatchEvent(new Event("DOMContentLoaded"));
  document.querySelector("button")!.click();
  expect(reload).toHaveBeenCalledOnce();
});

test("hydration cancels an early retry and later failures preserve unsent input", () => {
  const reload = install(); fail();
  window.dispatchEvent(new Event("ai-desktop-agent-ready"));
  document.body.innerHTML = '<textarea>保存前の指示</textarea>';
  fail(); vi.advanceTimersByTime(10000);
  expect(reload).not.toHaveBeenCalled();
  expect(sessionStorage.getItem(key)).toBeNull();
  expect(document.querySelector("textarea")).toHaveValue("保存前の指示");
});

test("hydration also removes deferred recovery and tolerates blocked storage cleanup", () => {
  sessionStorage.setItem(key, "1");
  vi.spyOn(document, "readyState", "get").mockReturnValue("loading");
  const reload = install(); fail();
  vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => { throw new DOMException("blocked"); });
  window.dispatchEvent(new Event("ai-desktop-agent-ready"));
  document.dispatchEvent(new Event("DOMContentLoaded"));
  expect(document.querySelector('[role="alert"]')).toBeNull();
  expect(reload).not.toHaveBeenCalled();
});

test("cleanup prevents a queued DOM-ready callback from replacing a newer page", () => {
  sessionStorage.setItem(key, "1");
  vi.spyOn(document, "readyState", "get").mockReturnValue("loading");
  install(); fail(); cleanups[0]();
  document.dispatchEvent(new Event("DOMContentLoaded"));
  expect(document.querySelector('[role="status"]')).toHaveTextContent("接続中");
});

test("ordinary runtime, style and external script failures cannot reload the app", () => {
  const reload = install();
  window.dispatchEvent(new ErrorEvent("error", { message: "runtime failure" }));
  const link = document.createElement("link"); document.body.append(link); link.dispatchEvent(new Event("error"));
  fail("https://external.test/_next/static/chunks/other.js");
  fail("/other.js");
  vi.advanceTimersByTime(10000);
  expect(reload).not.toHaveBeenCalled();
  expect(sessionStorage.getItem(key)).toBeNull();
});
