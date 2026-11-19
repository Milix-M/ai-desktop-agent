"""Bounded RFB decoding and server-owned manual input grants.

Only the project's None-security RFB 3.3/3.7/3.8 endpoints are supported.
Client messages may be fragmented or coalesced across WebSocket frames.
Unknown messages fail closed instead of guessing where the next message starts.
"""

import asyncio
from collections.abc import Callable

from ai_desktop_agent.server.websocket_io import MAX_MESSAGE_BYTES

IO_TIMEOUT = 10
MAX_HELD_KEYS = 256


class RfbInputError(ValueError):
    pass


class InputAuthority:
    """One manual-input interval; never re-enabled for another handoff."""

    def __init__(self):
        self.enabled = True
        self.clients: set[VncInput] = set()
        self.failed = False

    async def revoke(self):
        self.enabled = False
        results = await asyncio.gather(
            *(client.release() for client in tuple(self.clients)), return_exceptions=True
        )
        if self.failed or any(isinstance(result, BaseException) for result in results):
            raise RuntimeError("手動入力の解除を確認できませんでした")


class RfbMessages:
    def __init__(self):
        self.buffer = bytearray()
        self.stage = "version"
        self.authority: InputAuthority | None = None

    def feed(self, data: bytes, authority: InputAuthority | None):
        if not self.buffer:
            self.authority = authority
        self.buffer.extend(data)
        while self.buffer:
            size = self._size()
            if size is None or len(self.buffer) < size:
                return
            frame = bytes(self.buffer[:size])
            del self.buffer[:size]
            grant = self.authority
            self.authority = authority
            handshake = self.stage != "messages"
            if self.stage == "version":
                if frame not in {b"RFB 003.003\n", b"RFB 003.007\n", b"RFB 003.008\n"}:
                    raise RfbInputError("Unsupported RFB version")
                self.stage = "init" if frame == b"RFB 003.003\n" else "security"
            elif self.stage == "security":
                if frame != b"\x01":
                    raise RfbInputError("Unsupported RFB security")
                self.stage = "init"
            elif self.stage == "init":
                # A non-shared viewer can disconnect the agent's own VNC client.
                if frame != b"\x01":
                    raise RfbInputError("Shared RFB connection required")
                self.stage = "messages"
            yield frame, grant, handshake

    def _size(self):
        data = self.buffer
        if self.stage == "version":
            return 12
        if self.stage != "messages":
            return 1
        kind = data[0]
        if kind in {0, 3, 4, 150, 250}:
            return {0: 20, 3: 10, 4: 8, 150: 10, 250: 4}[kind]
        if kind == 5:
            return None if len(data) < 2 else 7 if data[1] & 0x80 else 6
        if kind == 255:
            if len(data) < 2:
                return None
            if data[1] != 0:
                raise RfbInputError("Unsupported RFB extension")
            return 12
        header = {2: 4, 6: 8, 248: 9, 251: 8}.get(kind)
        if header is None:
            raise RfbInputError("Unsupported RFB message")
        if len(data) < header:
            return None
        if kind == 2:
            size = 4 + 4 * int.from_bytes(data[2:4])
        elif kind == 6:
            size = 8 + abs(int.from_bytes(data[4:8], signed=True))
        elif kind == 248:
            if data[8] > 64:
                raise RfbInputError("RFB fence payload too large")
            size = 9 + data[8]
        else:
            size = 8 + 16 * data[6]
        if size > MAX_MESSAGE_BYTES:
            raise RfbInputError("RFB message too large")
        return size


class VncInput:
    def __init__(self, writer, current: Callable[[], InputAuthority | None]):
        self.writer = writer
        self.current = current
        self.messages = RfbMessages()
        self.authority: InputAuthority | None = None
        self.keys: dict[tuple[int, bytes], bytes] = {}
        self.pointer: bytes | None = None
        self._release_task: asyncio.Task | None = None

    def forward(self, data: bytes):
        current = self.current()
        for frame, grant, handshake in self.messages.feed(data, current):
            kind = frame[0]
            if not handshake and kind not in {0, 2, 3, 150, 248}:
                # Authorization belongs to the interval in which the message
                # began, and must still be current when it is forwarded.
                if grant is None or grant is not self.current() or not grant.enabled:
                    continue
                if self.authority is not grant:
                    if self.authority is not None:
                        raise RfbInputError("Input authority changed before release")
                    self.authority = grant
                    self._release_task = None
                    grant.clients.add(self)
                self._track(frame)
            self.writer.write(frame)

    def _track(self, frame: bytes):
        kind = frame[0]
        if kind in {4, 255}:
            key = (kind, frame[4:] if kind == 4 else frame[8:])
            down = frame[1] if kind == 4 else int.from_bytes(frame[2:4])
            if down:
                if key not in self.keys and len(self.keys) >= MAX_HELD_KEYS:
                    raise RfbInputError("Too many held RFB keys")
                self.keys[key] = frame
            else:
                self.keys.pop(key, None)
        elif kind == 5:
            held = frame[1] & 0x7F or (len(frame) == 7 and frame[6])
            self.pointer = frame if held else None

    async def release(self):
        if self._release_task is None:
            self._release_task = asyncio.create_task(self._release())
        await asyncio.shield(self._release_task)

    async def _release(self):
        grant = self.authority
        if grant is None:
            return
        try:
            for frame in self.keys.values():
                released = bytearray(frame)
                if frame[0] == 4:
                    released[1] = 0
                else:
                    released[2:4] = b"\x00\x00"
                self.writer.write(bytes(released))
            if self.pointer is not None:
                released = bytearray(self.pointer)
                released[1] &= 0x80
                if len(released) == 7:
                    released[6] = 0
                self.writer.write(bytes(released))
            if self.keys or self.pointer is not None:
                async with asyncio.timeout(IO_TIMEOUT):
                    await self.writer.drain()
        except Exception:
            grant.failed = True
            raise
        finally:
            self.keys.clear()
            self.pointer = None
            grant.clients.discard(self)
            self.authority = None
