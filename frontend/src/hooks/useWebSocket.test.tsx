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
  vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, json: async () => ({ authenticated: true }) } as Response);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("an old socket's failed auth check cannot lock a replacement connection", async () => {
  let resolve!: (value: Response) => void;
  vi.mocked(fetch).mockReturnValueOnce(new Promise(done => { resolve = done; }));
  const dispatch = vi.spyOn(window, "dispatchEvent");
  renderHook(() => useWebSocket(vi.fn()));
  act(() => { Socket.instances[0].open(); Socket.instances[0].close(); });
  await act(async () => vi.advanceTimersByTimeAsync(3000));
  act(() => Socket.instances[1].open());
  await act(async () => resolve({ ok: true, json: async () => ({ authenticated: false }) } as Response));
  expect(dispatch).not.toHaveBeenCalled();
});

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
    Socket.instances[0].open();
    Socket.instances[0].onmessage?.({ data: '{"type":"complete","success":false}' });
    Socket.instances[0].onclose?.();
  });
  await act(() => vi.advanceTimersByTimeAsync(5000));
  expect(Socket.instances).toHaveLength(2);
  expect(receive).not.toHaveBeenCalled();
});

it("suspends a malformed stream and accepts state only from its replacement connection", async () => {
  const receive = vi.fn();
  const disconnected = vi.fn();
  renderHook(() => useWebSocket(receive, disconnected));
  const socket = Socket.instances[0];
  act(() => {
    socket.open();
    socket.onmessage?.({ data: "truncated {" });
    socket.onmessage?.({ data: '{"type":"state","state":"executing"}' });
  });
  expect(receive).not.toHaveBeenCalled();
  expect(disconnected).toHaveBeenCalledOnce();
  expect(socket.close).toHaveBeenCalledWith(4002, "Invalid task-state message");
  await act(() => vi.advanceTimersByTimeAsync(3000));
  act(() => {
    Socket.instances[1].open();
    Socket.instances[1].onmessage?.({ data: '{"type":"snapshot","tasks":[]}' });
  });
  expect(receive).toHaveBeenCalledExactlyOnceWith({ type: "snapshot", tasks: [] });
});

it("does not send a heartbeat after the socket leaves its open state", async () => {
  renderHook(() => useWebSocket(vi.fn()));
  const socket = Socket.instances[0];
  act(() => { socket.open(); socket.readyState = 2; });
  await act(() => vi.advanceTimersByTimeAsync(20000));
  expect(socket.send).not.toHaveBeenCalled();
  expect(socket.close).not.toHaveBeenCalled();
  act(() => { socket.readyState = Socket.OPEN; });
  await act(() => vi.advanceTimersByTimeAsync(20000));
  expect(socket.send).toHaveBeenCalledExactlyOnceWith("ping");
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

it("notifies the current disconnect handler once on heartbeat failure and ignores late events", async () => {
  const receive = vi.fn();
  const initial = vi.fn();
  const latest = vi.fn();
  const { rerender } = renderHook(({ disconnected }) => useWebSocket(receive, disconnected), {
    initialProps: { disconnected: initial },
  });
  const first = Socket.instances[0];
  act(() => first.open());
  rerender({ disconnected: latest });
  await act(() => vi.advanceTimersByTimeAsync(30000));
  expect(initial).not.toHaveBeenCalled();
  expect(latest).toHaveBeenCalledOnce();
  act(() => {
    first.onmessage?.({ data: '{"type":"snapshot","tasks":[]}' });
    first.onclose?.();
    first.onopen?.();
  });
  expect(receive).not.toHaveBeenCalled();
  await act(() => vi.advanceTimersByTimeAsync(3000));
  expect(Socket.instances).toHaveLength(2);
  expect(latest).toHaveBeenCalledOnce();
});

it("does not publish disconnections from abandoned or unmounted connections", () => {
  const disconnected = vi.fn();
  const { unmount } = renderHook(() => useWebSocket(vi.fn(), disconnected), { wrapper: StrictMode });
  act(() => Socket.instances[0].onclose?.());
  expect(disconnected).not.toHaveBeenCalled();
  unmount();
  act(() => Socket.instances[1].onclose?.());
  expect(disconnected).not.toHaveBeenCalled();
});
