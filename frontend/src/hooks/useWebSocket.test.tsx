import { StrictMode } from "react";
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
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
  open() { this.readyState = 1; this.onopen?.(); }
}

beforeEach(() => {
  vi.useFakeTimers();
  Socket.instances = [];
  vi.stubGlobal("WebSocket", Socket);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it("reconnects and delivers the new server snapshot", async () => {
  const receive = vi.fn();
  renderHook(() => useWebSocket(receive));
  const first = Socket.instances[0];
  act(() => { first.open(); first.close(); });
  await act(() => vi.advanceTimersByTimeAsync(3000));
  expect(Socket.instances).toHaveLength(2);
  const second = Socket.instances[1];
  const snapshot = { type: "snapshot", tasks: [] };
  act(() => { second.open(); second.onmessage?.({ data: JSON.stringify(snapshot) }); });
  expect(receive).toHaveBeenCalledWith(snapshot);
});

it("never reconnects after unmount, even if close arrives later", async () => {
  const { unmount } = renderHook(() => useWebSocket(vi.fn()));
  const first = Socket.instances[0];
  unmount();
  act(() => first.onclose?.());
  await act(() => vi.advanceTimersByTimeAsync(60000));
  expect(Socket.instances).toHaveLength(1);
});

it("ignores messages and closes from StrictMode's abandoned socket", async () => {
  const receive = vi.fn();
  renderHook(() => useWebSocket(receive), { wrapper: StrictMode });
  expect(Socket.instances).toHaveLength(2);
  act(() => {
    Socket.instances[0].onmessage?.({ data: '{"type":"complete","success":false}' });
    Socket.instances[0].onclose?.();
  });
  await act(() => vi.advanceTimersByTimeAsync(5000));
  expect(Socket.instances).toHaveLength(2);
  expect(receive).not.toHaveBeenCalled();
});

it("detects an unresponsive connection through ping timeout", async () => {
  renderHook(() => useWebSocket(vi.fn()));
  const first = Socket.instances[0];
  act(() => first.open());
  await act(() => vi.advanceTimersByTimeAsync(20000));
  expect(first.send).toHaveBeenCalledWith("ping");
  await act(() => vi.advanceTimersByTimeAsync(10000));
  expect(first.close).toHaveBeenCalledOnce();
  await act(() => vi.advanceTimersByTimeAsync(3000));
  expect(Socket.instances).toHaveLength(2);
});

it("keeps a responsive connection and uses the latest callback", async () => {
  const firstHandler = vi.fn();
  const secondHandler = vi.fn();
  const { rerender } = renderHook(({ handler }) => useWebSocket(handler), { initialProps: { handler: firstHandler } });
  const socket = Socket.instances[0];
  act(() => socket.open());
  rerender({ handler: secondHandler });
  await act(() => vi.advanceTimersByTimeAsync(20000));
  act(() => socket.onmessage?.({ data: '{"type":"pong"}' }));
  await act(() => vi.advanceTimersByTimeAsync(10000));
  expect(socket.close).not.toHaveBeenCalled();
  expect(firstHandler).not.toHaveBeenCalled();
  expect(secondHandler).toHaveBeenCalledWith({ type: "pong" });
});
