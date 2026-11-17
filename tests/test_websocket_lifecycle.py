import asyncio
from types import SimpleNamespace

import pytest
from anyio import create_task_group
from starlette.websockets import WebSocketDisconnect

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server import websocket_io
from ai_desktop_agent.server.store import TaskStore


class Viewer:
    def __init__(self, fault=None):
        self.fault = fault
        self.incoming = asyncio.Queue()
        self.sent = asyncio.Queue()
        self.sending = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.read_cancelled = asyncio.Event()
        self.write_cancelled = asyncio.Event()
        self.close_codes = []

    async def accept(self):
        pass

    async def send_json(self, payload):
        self.sending.set()
        if self.fault == "write-error":
            raise OSError("peer stopped reading")
        if self.fault in {"write-timeout", "queue-overload"}:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.write_cancelled.set()
                raise
        await self.sent.put(payload)

    async def receive(self):
        try:
            return await self.incoming.get()
        except asyncio.CancelledError:
            self.read_cancelled.set()
            raise

    async def receive_text(self):
        message = await self.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect()
        return message["text"]

    async def close(self, code=1000):
        self.close_codes.append(code)
        self.closed.set()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    for name in ("_ws_state_cbs", "_ws_action_cbs", "_ws_error_cbs", "_ws_complete_cbs"):
        monkeypatch.setattr(server, name, [])


def assert_no_registrations():
    assert not server._ws_state_cbs
    assert not server._ws_action_cbs
    assert not server._ws_error_cbs
    assert not server._ws_complete_cbs


@pytest.mark.parametrize("fault", ["write-error", "write-timeout", "queue-overload"])
async def test_delivery_failure_cancels_receive_and_releases_callbacks_without_peer_cooperation(
    store, monkeypatch, fault
):
    original = server._send_json

    async def bounded_send(ws, payload):
        return await original(ws, payload, timeout=0.03)

    monkeypatch.setattr(server, "_send_json", bounded_send)
    viewer = Viewer(fault)
    endpoint = asyncio.create_task(server.websocket_endpoint(viewer))
    try:
        await asyncio.wait_for(viewer.sending.wait(), 1)
        if fault == "queue-overload":
            session = SimpleNamespace(id="task", vm_id="desktop", session_order=1)
            for _ in range(300):
                await server._fanout(server._ws_error_cbs, session, "controlled backlog")
            viewer.release.set()
        await asyncio.wait_for(asyncio.shield(endpoint), 0.5)
        assert 1013 in viewer.close_codes
        assert viewer.read_cancelled.is_set()
        if fault == "write-timeout":
            assert viewer.write_cancelled.is_set()
        assert_no_registrations()
    finally:
        viewer.release.set()
        endpoint.cancel()
        await asyncio.gather(endpoint, return_exceptions=True)


@pytest.mark.parametrize(
    "message",
    [
        {"type": "websocket.receive", "text": "unsupported"},
        {"type": "websocket.receive", "bytes": b"ping"},
    ],
)
async def test_invalid_control_messages_close_explicitly_and_release_callbacks(store, message):
    viewer = Viewer()
    endpoint = asyncio.create_task(server.websocket_endpoint(viewer))
    try:
        assert (await asyncio.wait_for(viewer.sent.get(), 1))["type"] == "snapshot"
        await viewer.incoming.put(message)
        await asyncio.wait_for(asyncio.shield(endpoint), 0.3)
        assert 1003 in viewer.close_codes
        assert_no_registrations()
    finally:
        endpoint.cancel()
        await asyncio.gather(endpoint, return_exceptions=True)


async def test_a_stalled_close_is_bounded_after_receive_and_callbacks_are_released(
    store, monkeypatch
):
    monkeypatch.setattr(websocket_io, "CLOSE_TIMEOUT", 0.03)
    viewer = Viewer("write-error")
    close_cancelled = asyncio.Event()

    async def close(code=1000):
        assert_no_registrations()
        assert viewer.read_cancelled.is_set()
        viewer.close_codes.append(code)
        try:
            await asyncio.Event().wait()
        finally:
            close_cancelled.set()

    viewer.close = close
    await asyncio.wait_for(server.websocket_endpoint(viewer), 0.5)
    assert close_cancelled.is_set()
    assert viewer.close_codes == [1013]


async def test_cancellation_releases_both_pumps_and_all_registrations(store):
    viewer = Viewer("write-timeout")
    endpoint = asyncio.create_task(server.websocket_endpoint(viewer))
    await asyncio.wait_for(viewer.sending.wait(), 1)
    endpoint.cancel()
    with pytest.raises(asyncio.CancelledError):
        await endpoint
    assert viewer.write_cancelled.is_set()
    assert viewer.read_cancelled.is_set()
    assert_no_registrations()


async def test_a_peer_disconnect_cancels_a_blocked_send(store):
    viewer = Viewer("write-timeout")
    endpoint = asyncio.create_task(server.websocket_endpoint(viewer))
    try:
        await asyncio.wait_for(viewer.sending.wait(), 1)
        await viewer.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(asyncio.shield(endpoint), 0.5)
        assert viewer.write_cancelled.is_set()
        assert_no_registrations()
    finally:
        endpoint.cancel()
        await asyncio.gather(endpoint, return_exceptions=True)


async def test_ping_still_receives_pong_and_keeps_the_connection_available(store):
    viewer = Viewer()
    endpoint = asyncio.create_task(server.websocket_endpoint(viewer))
    try:
        assert (await asyncio.wait_for(viewer.sent.get(), 1))["type"] == "snapshot"
        for _ in range(3):
            await viewer.incoming.put({"type": "websocket.receive", "text": "ping"})
            assert await asyncio.wait_for(viewer.sent.get(), 1) == {"type": "pong"}
        assert not endpoint.done() and not viewer.closed.is_set()
    finally:
        endpoint.cancel()
        await asyncio.gather(endpoint, return_exceptions=True)


async def test_close_does_not_swallow_external_cancellation():
    class Closing:
        async def close(self, code):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await websocket_io.close_websocket(Closing())


async def test_asgi_cancel_scope_cannot_interrupt_cleanup_halfway(store):
    viewer = Viewer("write-timeout")
    async with create_task_group() as group:
        group.start_soon(server.websocket_endpoint, viewer)
        await asyncio.wait_for(viewer.sending.wait(), 1)
        group.cancel_scope.cancel()
    assert viewer.read_cancelled.is_set()
    assert viewer.write_cancelled.is_set()
    assert viewer.closed.is_set()
    assert_no_registrations()
