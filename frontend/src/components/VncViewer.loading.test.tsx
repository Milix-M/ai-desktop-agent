import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

class LoadedRFB extends EventTarget {
  viewOnly = false;
  disconnect = vi.fn();
}

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.resetModules(); vi.doUnmock("@novnc/novnc"); });

test("a failed viewer chunk can be loaded again after the connection returns", async () => {
  vi.resetModules();
  vi.doMock("@novnc/novnc", () => { throw new TypeError("viewer chunk unavailable"); });
  const errors = vi.spyOn(console, "error").mockImplementation(() => {});
  const { default: Viewer } = await import("./VncViewer");
  const onChange = vi.fn();
  vi.useFakeTimers();
  const view = render(<Viewer onConnectionChange={onChange} allowInput />);
  await vi.waitFor(() => expect(errors).toHaveBeenCalled());
  const constructed = vi.fn(function () { return new LoadedRFB(); });
  vi.doMock("@novnc/novnc", () => ({ default: constructed }));
  vi.resetModules();
  await act(() => vi.advanceTimersByTimeAsync(3000));
  await vi.waitFor(() => expect(constructed).toHaveBeenCalledOnce());
  const connection = constructed.mock.results[0].value as LoadedRFB;
  expect(connection.viewOnly).toBe(false);
  view.unmount();
  expect(connection.disconnect).toHaveBeenCalledOnce();
  expect(vi.getTimerCount()).toBe(0);
});

test("a chunk failure after switching away cannot schedule retries or notify the old viewer", async () => {
  vi.resetModules();
  let fail!: (reason: Error) => void;
  const started = vi.fn();
  const pending = new Promise<never>((_resolve, reject) => { fail = reject; });
  vi.doMock("@novnc/novnc", async () => { started(); await pending; return { default: LoadedRFB }; });
  const errors = vi.spyOn(console, "error").mockImplementation(() => {});
  const { default: Viewer } = await import("./VncViewer");
  const onChange = vi.fn();
  const { unmount } = render(<Viewer onConnectionChange={onChange} />);
  await waitFor(() => expect(started).toHaveBeenCalledOnce());
  vi.useFakeTimers();
  unmount();
  const calls = onChange.mock.calls.length;
  await act(async () => { fail(new Error("late chunk failure")); });
  await act(() => vi.advanceTimersByTimeAsync(10000));
  expect(onChange).toHaveBeenCalledTimes(calls);
  expect(errors).not.toHaveBeenCalled();
  expect(vi.getTimerCount()).toBe(0);
});
