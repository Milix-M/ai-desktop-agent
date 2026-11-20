import asyncio
import struct

import pytest

from ai_desktop_agent.server import vnc_input
from ai_desktop_agent.server.vnc_input import InputAuthority, RfbInputError, VncInput

HANDSHAKE = b"RFB 003.008\n\x01\x01"


def key(symbol=0xFFE3, down=True, code=None):
    if code is None:
        return struct.pack(">BBHI", 4, down, 0, symbol)
    return struct.pack(">BBHII", 255, 0, down, symbol, code)


def pointer(mask=1, extended=False):
    message = struct.pack(">BBHH", 5, (mask & 0x7F) | (0x80 if extended else 0), 320, 240)
    return message + bytes([mask >> 7]) if extended else message


def clipboard(data, extended=False):
    return struct.pack(">B3xi", 6, -len(data) if extended else len(data)) + data


class Writer:
    def __init__(self):
        self.frames = []
        self.drains = 0

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        self.drains += 1


@pytest.mark.parametrize("version", [b"003.003", b"003.007", b"003.008"])
@pytest.mark.parametrize("chunk", [1, 5, 1024])
def test_readonly_connection_preserves_screen_protocol_and_drops_all_input(version, chunk):
    writer = Writer()
    connection = VncInput(writer, lambda: None)
    handshake = b"RFB " + version + b"\n" + (b"\x01" if version == b"003.003" else b"\x01\x01")
    controls = [
        b"\0" * 20,  # SetPixelFormat
        b"\x02\0\0\x02" + b"\0" * 8,  # SetEncodings
        b"\x03" + b"\0" * 9,  # FramebufferUpdateRequest
        b"\x96" + b"\0" * 9,  # EnableContinuousUpdates
        b"\xf8" + b"\0" * 7 + b"\x04sync",  # Fence
    ]
    mutations = [
        key(),
        key(code=29),
        pointer(),
        pointer(256, True),
        clipboard(b"secret" + key()),
        clipboard(b"\x01\0\0\x01", True),
        b"\xfa\0\x01\x02",  # XVP
        b"\xfb\0\x04\0\x03\0\x01\0" + b"\0" * 16,  # SetDesktopSize
    ]
    stream = handshake + b"".join(controls + mutations + controls)
    for offset in range(0, len(stream), chunk):
        connection.forward(stream[offset : offset + chunk])
    assert b"".join(writer.frames) == handshake + b"".join(controls * 2)
    assert not connection.keys and connection.pointer is None


@pytest.mark.parametrize("extended", [False, True])
async def test_revocation_releases_only_held_input_from_every_viewer_and_keeps_video(extended):
    authority = InputAuthority()
    current = [authority]
    viewers = [VncInput(Writer(), lambda: current[0]) for _ in range(2)]
    pressed = key(code=29 if extended else None)
    released = key(down=False, code=29 if extended else None)
    mouse = pointer(256 if extended else 1, extended)
    no_buttons = pointer(0, extended)
    for viewer in viewers:
        viewer.forward(HANDSHAKE + pressed + pressed + mouse + clipboard(b"typed"))
        assert len(viewer.keys) == 1
    await authority.revoke()
    for viewer in viewers:
        assert viewer.writer.frames[-2:] == [released, no_buttons]
        assert viewer.writer.drains == 1
        assert not viewer.keys and viewer.pointer is None
        initial = len(viewer.writer.frames)
        viewer.forward(pressed + mouse + clipboard(b"blocked"))
        assert len(viewer.writer.frames) == initial
        viewer.forward(b"\x03" + b"\0" * 9)
        assert viewer.writer.frames[-1] == b"\x03" + b"\0" * 9
    assert not authority.clients
    current[0] = InputAuthority()
    viewers[0].forward(pressed + released + mouse + no_buttons)
    assert viewers[0].writer.frames[-4:] == [pressed, released, mouse, no_buttons]
    await current[0].revoke()
    assert viewers[0].writer.drains == 1  # Already released, no duplicate key-up.


@pytest.mark.parametrize("initial", [None, "old"])
def test_a_partial_input_cannot_be_authorized_by_a_later_handoff(initial):
    old = InputAuthority() if initial else None
    current = [old]
    viewer = VncInput(Writer(), lambda: current[0])
    viewer.forward(HANDSHAKE)
    pressed = key()
    viewer.forward(pressed[:1])
    current[0] = InputAuthority()
    viewer.forward(pressed[1:] + pressed)
    assert viewer.writer.frames == [b"RFB 003.008\n", b"\x01", b"\x01", pressed]
    assert viewer.authority is current[0]


@pytest.mark.parametrize(
    "prefix,invalid",
    [
        (b"", b"RFB 003.009\n"),
        (b"RFB 003.008\n", b"\x02"),
        (b"RFB 003.008\n\x01", b"\x00"),
        (HANDSHAKE, b"\x01"),
        (HANDSHAKE, b"\xff\x01"),
        (HANDSHAKE, b"\xf8" + b"\0" * 7 + b"\x41"),
        (HANDSHAKE, struct.pack(">B3xi", 6, 1024 * 1024)),
        (HANDSHAKE, struct.pack(">B3xi", 6, -(1024 * 1024))),
        (HANDSHAKE, struct.pack(">B3xi", 6, -(2**31))),
    ],
)
def test_unsupported_and_oversized_protocol_is_rejected_before_payload_forwarding(prefix, invalid):
    writer = Writer()
    viewer = VncInput(writer, lambda: None)
    viewer.forward(prefix)
    accepted = b"".join(writer.frames)
    with pytest.raises(RfbInputError):
        viewer.forward(invalid + key())
    assert b"".join(writer.frames) == accepted


async def test_distinct_key_limit_bounds_memory_and_all_accepted_presses_are_released(monkeypatch):
    monkeypatch.setattr(vnc_input, "MAX_HELD_KEYS", 2)
    authority = InputAuthority()
    viewer = VncInput(Writer(), lambda: authority)
    viewer.forward(HANDSHAKE + key(65) + key(66) + key(65))
    with pytest.raises(RfbInputError, match="Too many"):
        viewer.forward(key(67))
    assert len(viewer.keys) == 2
    await authority.revoke()
    assert viewer.writer.frames[-2:] == [key(65, False), key(66, False)]


@pytest.mark.parametrize("failure", [ConnectionError, TimeoutError, "stalled"])
async def test_failed_release_is_retained_after_disconnection_and_prevents_resumption(
    monkeypatch, failure
):
    authority = InputAuthority()
    viewer = VncInput(Writer(), lambda: authority)
    viewer.forward(HANDSHAKE + key())

    async def fail():
        if failure == "stalled":
            await asyncio.Event().wait()
        raise failure("unacknowledged release")

    monkeypatch.setattr(vnc_input, "IO_TIMEOUT", 0.01)
    viewer.writer.drain = fail
    with pytest.raises(TimeoutError if failure == "stalled" else failure):
        await viewer.release()
    assert not authority.clients and authority.failed
    with pytest.raises(RuntimeError, match="解除を確認"):
        await authority.revoke()


async def test_a_new_grant_cannot_take_ownership_of_keys_held_in_an_unreleased_old_grant():
    old = InputAuthority()
    current = [old]
    viewer = VncInput(Writer(), lambda: current[0])
    viewer.forward(HANDSHAKE + key())
    current[0] = InputAuthority()
    with pytest.raises(RfbInputError, match="authority changed"):
        viewer.forward(key(65))
    assert viewer.authority is old and not current[0].clients
    await old.revoke()
    assert viewer.writer.frames[-1] == key(down=False)
    viewer.forward(key(65))
    assert viewer.authority is current[0]
    await current[0].revoke()


async def test_revocation_waits_for_a_pending_disconnect_release_without_duplicate_messages():
    authority = InputAuthority()
    viewer = VncInput(Writer(), lambda: authority)
    viewer.forward(HANDSHAKE + key() + pointer())
    started = asyncio.Event()
    unblock = asyncio.Event()

    async def slow_drain():
        started.set()
        await unblock.wait()

    viewer.writer.drain = slow_drain
    disconnect = asyncio.create_task(viewer.release())
    await asyncio.wait_for(started.wait(), 1)
    revoke = asyncio.create_task(authority.revoke())
    await asyncio.sleep(0)
    assert not revoke.done()
    disconnect.cancel()
    with pytest.raises(asyncio.CancelledError):
        await disconnect
    assert not revoke.done()
    assert viewer.writer.frames[-2:] == [key(down=False), pointer(0)]
    unblock.set()
    await asyncio.wait_for(revoke, 1)
    assert not authority.clients


async def test_a_release_failure_does_not_prevent_other_viewers_from_releasing_input():
    authority = InputAuthority()
    viewers = [VncInput(Writer(), lambda: authority) for _ in range(2)]
    for viewer in viewers:
        viewer.forward(HANDSHAKE + key())

    def fail(data):
        raise ConnectionError("gone")

    viewers[0].writer.write = fail
    with pytest.raises(RuntimeError, match="解除を確認"):
        await authority.revoke()
    assert viewers[1].writer.frames[-1] == key(down=False)
    assert not authority.clients
