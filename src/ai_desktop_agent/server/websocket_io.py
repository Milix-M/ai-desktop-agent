"""Shared bounds for WebSocket decoding and best-effort shutdown."""

import asyncio
import contextlib

MAX_MESSAGE_BYTES = 1024 * 1024
MAX_MESSAGE_QUEUE = 4
CLOSE_TIMEOUT = 5.0


async def close_websocket(ws, code: int = 1000) -> None:
    with contextlib.suppress(Exception):
        async with asyncio.timeout(CLOSE_TIMEOUT):
            await ws.close(code=code)
