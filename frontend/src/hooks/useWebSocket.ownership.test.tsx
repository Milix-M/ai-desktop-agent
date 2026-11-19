import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { useWebSocket } from "./useWebSocket";

class Socket {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  send = vi.fn();
  close = vi.fn(() => { this.readyState = 3; this.onclose?.(); });
  constructor() { Socket.instances.push(this); }
  open() { this.readyState = Socket.OPEN; this.onopen?.(); }
}

beforeEach(() => {
  vi.useFakeTimers(); Socket.instances = [];
  vi.stubGlobal("WebSocket", Socket);
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response('{"authenticated":true}'));
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

test("a disconnect subscriber closing the workspace leaves no reconnect timer", async () => {
  const disconnected = vi.fn(() => hook.unmount());
  const hook = renderHook(() => useWebSocket(vi.fn(), disconnected));
  const owned = Socket.instances[0];
  act(() => owned.open());
  act(() => owned.close());
  expect(disconnected).toHaveBeenCalledOnce();
  expect(vi.getTimerCount()).toBe(0);
  await act(() => vi.advanceTimersByTimeAsync(60000));
  expect(Socket.instances).toHaveLength(1);
  expect(owned.send).not.toHaveBeenCalled();
  expect(fetch).not.toHaveBeenCalled();
});

test.each(["clear", "replace"])("cleanup retains ownership when a consumer changes the returned connection ref: %s", async change => {
  const receive = vi.fn();
  const disconnected = vi.fn();
  const hook = renderHook(() => useWebSocket(receive, disconnected));
  const owned = Socket.instances[0];
  let external: WebSocket | null = null;
  try {
    act(() => owned.open());
    await act(() => vi.advanceTimersByTimeAsync(20000));
    expect(owned.send).toHaveBeenCalledExactlyOnceWith("ping");
    if (change === "replace") external = new WebSocket("ws://consumer-owned");
    // The hook returns this writable ref as its public API. No hook-private
    // state or event/timer callback is replaced or invoked after cancellation.
    act(() => { hook.result.current.current = external; });
    await act(() => vi.advanceTimersByTimeAsync(10000));
    expect(disconnected).not.toHaveBeenCalled();
    expect(owned.close).not.toHaveBeenCalled();
    hook.unmount();
    expect({
      ownedCloses: owned.close.mock.calls.length,
      externalCloses: external ? vi.mocked(external.close).mock.calls.length : 0,
    }).toEqual({ ownedCloses: 1, externalCloses: 0 });
    await act(() => vi.advanceTimersByTimeAsync(60000));
    expect(owned.send).toHaveBeenCalledTimes(1);
    expect(Socket.instances).toHaveLength(change === "replace" ? 2 : 1);
    expect(receive).not.toHaveBeenCalled();
  } finally {
    hook.unmount();
    owned.close();
    external?.close();
  }
});
