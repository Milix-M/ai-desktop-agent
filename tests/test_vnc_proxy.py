import asyncio
from functools import partial
from types import SimpleNamespace

import pytest
from anyio import create_task_group

from ai_desktop_agent.server import websocket_io
from ai_desktop_agent.server.vnc_input import InputAuthority
from ai_desktop_agent.server.vnc_proxy import proxy_vnc


class Browser:
    scope = {"subprotocols": ["binary"]}

    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.closed = []
        self.accepted = None

    async def accept(self, subprotocol):
        self.accepted = subprotocol

    async def receive(self):
        return await self.incoming.get()

    async def send_bytes(self, data):
        await self.outgoing.put(data)

    async def close(self, code=1000):
        self.closed.append(code)


async def test_binary_vnc_transport_and_browser_disconnect_release_the_tcp_socket():
    peer_closed = asyncio.Event()

    async def desktop(reader, writer):
        try:
            writer.write(b"RFB 003.008\n")
            await writer.drain()
            data = await reader.readexactly(12)
            writer.write(b"reply:" + data)
            await writer.drain()
            assert await reader.read() == b""
        finally:
            writer.close()
            await writer.wait_closed()
            peer_closed.set()

    async with await asyncio.start_server(desktop, "127.0.0.1", 0) as server:
        browser = Browser()
        port = server.sockets[0].getsockname()[1]
        task = asyncio.create_task(proxy_vnc(browser, "127.0.0.1", port))
        try:
            assert await asyncio.wait_for(browser.outgoing.get(), 1) == b"RFB 003.008\n"
            assert browser.accepted == "binary"
            await browser.incoming.put({"type": "websocket.receive", "bytes": b"RFB 003.008\n"})
            assert await asyncio.wait_for(browser.outgoing.get(), 1) == b"reply:RFB 003.008\n"
            await browser.incoming.put({"type": "websocket.disconnect"})
            await asyncio.wait_for(task, 1)
            await asyncio.wait_for(peer_closed.wait(), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "message,code",
    [
        ({"type": "websocket.receive", "text": "no text"}, 1003),
        ({"type": "websocket.receive", "bytes": b"invalid RFB!"}, 1003),
        ({"type": "websocket.receive", "bytes": b"x" * (1024 * 1024 + 1)}, 1009),
    ],
)
async def test_invalid_frames_close_without_forwarding_to_desktop(message, code):
    received = []
    finished = asyncio.Event()

    async def desktop(reader, writer):
        received.append(await reader.read())
        writer.close()
        await writer.wait_closed()
        finished.set()

    async with await asyncio.start_server(desktop, "127.0.0.1", 0) as server:
        browser = Browser()
        await browser.incoming.put(message)
        await asyncio.wait_for(
            proxy_vnc(browser, "127.0.0.1", server.sockets[0].getsockname()[1]), 1
        )
        assert code in browser.closed
        await asyncio.wait_for(finished.wait(), 1)
        assert received == [b""]


@pytest.mark.parametrize(
    "message,code",
    [
        ({"type": "websocket.disconnect"}, 1000),
        ({"type": "websocket.receive", "text": "unsupported"}, 1003),
        ({"type": "websocket.receive", "bytes": b"x" * (1024 * 1024 + 1)}, 1009),
    ],
)
async def test_stalled_browser_close_cannot_keep_the_desktop_socket_open(
    monkeypatch, message, code
):
    monkeypatch.setattr(websocket_io, "CLOSE_TIMEOUT", 0.03)
    peer_closed = asyncio.Event()
    close_cancelled = asyncio.Event()

    async def desktop(reader, writer):
        try:
            writer.write(b"ready")
            await writer.drain()
            assert await reader.read() == b""
        finally:
            writer.close()
            await writer.wait_closed()
            peer_closed.set()

    class StubbornBrowser(Browser):
        async def close(self, code=1000):
            self.closed.append(code)
            try:
                await asyncio.Event().wait()
            finally:
                close_cancelled.set()

    async with await asyncio.start_server(desktop, "127.0.0.1", 0) as server:
        browser = StubbornBrowser()
        task = asyncio.create_task(
            proxy_vnc(browser, "127.0.0.1", server.sockets[0].getsockname()[1])
        )
        try:
            assert await asyncio.wait_for(browser.outgoing.get(), 1) == b"ready"
            await browser.incoming.put(message)
            await asyncio.wait_for(asyncio.shield(task), 0.5)
            assert close_cancelled.is_set()
            await asyncio.wait_for(peer_closed.wait(), 1)
            assert browser.closed == [code]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_a_binary_message_at_the_size_limit_is_forwarded_completely():
    payload = b"\x06\0\0\0" + (1024 * 1024 - 8).to_bytes(4) + b"x" * (1024 * 1024 - 8)
    handshake = b"RFB 003.008\n\x01\x01"
    authority = InputAuthority()
    received = asyncio.Event()

    async def desktop(reader, writer):
        try:
            assert await reader.readexactly(len(handshake) + len(payload)) == handshake + payload
            received.set()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(desktop, "127.0.0.1", 0) as server:
        browser = Browser()
        await browser.incoming.put({"type": "websocket.receive", "bytes": handshake})
        await browser.incoming.put({"type": "websocket.receive", "bytes": payload})
        task = asyncio.create_task(
            proxy_vnc(
                browser,
                "127.0.0.1",
                server.sockets[0].getsockname()[1],
                input_authority=lambda: authority,
            )
        )
        try:
            await asyncio.wait_for(received.wait(), 1)
            await browser.incoming.put({"type": "websocket.disconnect"})
            await asyncio.wait_for(task, 1)
            assert browser.closed == [1000]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("state", ["other-vm", "finished", "running", "resumed", "manual"])
async def test_endpoint_uses_only_its_own_current_manual_input_authority(monkeypatch, state):
    from ai_desktop_agent.server import app, vnc_proxy

    vm = SimpleNamespace(id="desktop", vnc_host="127.0.0.1")
    authority = InputAuthority()
    pending = asyncio.Event()
    if state == "resumed":
        pending.set()  # Consent accepted, but the session has not yielded yet.
    unrelated = SimpleNamespace(
        vm_id="another-desktop",
        is_running=True,
        _manual_resume=asyncio.Event(),
        _input_authority=InputAuthority(),
    )
    matching = SimpleNamespace(
        vm_id="other" if state == "other-vm" else vm.id,
        is_running=state != "finished",
        _manual_resume=None if state == "running" else pending,
        _input_authority=authority,
    )
    monkeypatch.setattr(app, "_sessions", {"unrelated": unrelated, "matching": matching})
    monkeypatch.setattr(app, "_resolve_vm", lambda *args, **kwargs: vm)
    received = []
    peer_closed = asyncio.Event()

    async def desktop(reader, writer):
        try:
            writer.write(b"RFB 003.008\n")
            await writer.drain()
            received.append(await reader.read())
        finally:
            writer.close()
            await writer.wait_closed()
            peer_closed.set()

    handshake = b"RFB 003.008\n\x01\x01"
    pressed = b"\x04\x01\0\0\0\0\xff\xe3"
    released = b"\x04\0\0\0\0\0\xff\xe3"
    update = b"\x03" + b"\0" * 9
    async with await asyncio.start_server(desktop, "127.0.0.1", 0) as server:
        monkeypatch.setattr(
            vnc_proxy,
            "proxy_vnc",
            partial(proxy_vnc, port=server.sockets[0].getsockname()[1]),
        )
        browser = Browser()
        endpoint = asyncio.create_task(app.vnc_stream(browser, vm.id))
        try:
            assert await asyncio.wait_for(browser.outgoing.get(), 1) == b"RFB 003.008\n"
            await browser.incoming.put(
                {"type": "websocket.receive", "bytes": handshake + pressed + update}
            )
            await browser.incoming.put({"type": "websocket.disconnect"})
            await asyncio.wait_for(endpoint, 1)
            await asyncio.wait_for(peer_closed.wait(), 1)
            expected = handshake + (pressed if state == "manual" else b"") + update
            if state == "manual":
                expected += released
            assert received == [expected]
            assert not authority.clients and not unrelated._input_authority.clients
            assert browser.closed == [1000]
        finally:
            endpoint.cancel()
            await asyncio.gather(endpoint, return_exceptions=True)


async def test_asgi_cancel_scope_still_releases_the_desktop_socket():
    peer_closed = asyncio.Event()

    async def desktop(reader, writer):
        try:
            writer.write(b"ready")
            await writer.drain()
            assert await reader.read() == b""
        finally:
            writer.close()
            await writer.wait_closed()
            peer_closed.set()

    async with await asyncio.start_server(desktop, "127.0.0.1", 0) as server:
        browser = Browser()
        async with create_task_group() as group:
            group.start_soon(proxy_vnc, browser, "127.0.0.1", server.sockets[0].getsockname()[1])
            assert await asyncio.wait_for(browser.outgoing.get(), 1) == b"ready"
            group.cancel_scope.cancel()
        assert browser.closed == [1000]
        await asyncio.wait_for(peer_closed.wait(), 1)


async def test_refused_desktop_connection_closes_browser_without_accepting_it():
    import socket

    # A bound, non-listening port refuses TCP without racing a closed ephemeral port.
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        browser = Browser()
        await asyncio.wait_for(proxy_vnc(browser, "127.0.0.1", unavailable.getsockname()[1]), 1)
    assert browser.accepted is None and browser.closed == [1000]
    assert browser.outgoing.empty()
