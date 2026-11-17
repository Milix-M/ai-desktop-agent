import asyncio

import pytest

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
            data = await reader.readexactly(4)
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
            await browser.incoming.put({"type": "websocket.receive", "bytes": b"keys"})
            assert await asyncio.wait_for(browser.outgoing.get(), 1) == b"reply:keys"
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
