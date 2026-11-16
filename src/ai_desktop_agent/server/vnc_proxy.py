"""Authenticated binary WebSocket to a project-owned environment's VNC socket."""

import asyncio
import contextlib
import logging

from starlette.websockets import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
MAX_MESSAGE_BYTES = 1024 * 1024
IO_TIMEOUT = 10


async def proxy_vnc(ws: WebSocket, host: str, port: int = 5900) -> None:
    writer = None
    pumps: list[asyncio.Task] = []
    try:
        async with asyncio.timeout(IO_TIMEOUT):
            reader, writer = await asyncio.open_connection(host, port)
        await ws.accept(
            subprotocol="binary" if "binary" in ws.scope.get("subprotocols", []) else None
        )

        async def to_browser():
            while data := await reader.read(65536):
                async with asyncio.timeout(IO_TIMEOUT):
                    await ws.send_bytes(data)

        async def to_desktop():
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                data = message.get("bytes")
                if data is None:
                    await ws.close(1003)
                    return
                if len(data) > MAX_MESSAGE_BYTES:
                    await ws.close(1009)
                    return
                writer.write(data)
                async with asyncio.timeout(IO_TIMEOUT):
                    await writer.drain()

        pumps = [asyncio.create_task(to_browser()), asyncio.create_task(to_desktop())]
        done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    except (WebSocketDisconnect, ConnectionError, OSError, TimeoutError):
        logger.debug("画面配信を終了: %s", host, exc_info=True)
    finally:
        for task in pumps:
            task.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                async with asyncio.timeout(IO_TIMEOUT):
                    await writer.wait_closed()
        with contextlib.suppress(RuntimeError, OSError):
            await ws.close()
