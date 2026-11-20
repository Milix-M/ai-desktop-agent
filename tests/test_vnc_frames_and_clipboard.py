"""Real RFB frames and clipboard failures obey the display contract."""

import struct
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from PIL import Image
from vncdotool.client import RGB24
from vncdotool.rfb import Encoding

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.clipboard import REQUEST, TEXT
from tests.http import AuthenticatedClient
from tests.test_clipboard import negotiate, server_message
from tests.test_vnc_gestures import events
from tests.test_vnc_gestures import wire as wire


@pytest.fixture
def frames(wire):
    protocol = wire.protocol
    protocol.width, protocol.height = 4, 3
    protocol.pixel_format, protocol.image_mode = RGB24, "RGB"
    protocol._handler = protocol._handleExpected
    protocol.expect(protocol._handleConnection, 1)
    sent, write = [], wire.transport.write
    pixels = bytes(value for y in range(3) for x in range(4) for value in (30 * x, 60 * y, 17))

    def respond(data):
        sent.append(data)
        write(data)
        if data[0] == 3:
            protocol.dataReceived(
                struct.pack("!BBH", 0, 0, 1)
                + struct.pack("!HHHHi", 0, 0, 4, 3, Encoding.RAW)
                + pixels
            )

    wire.transport.write = respond
    wire.sent = sent
    return wire


@pytest.mark.parametrize("region", [(1, 1, 2, 1), (0, 0, 4, 3), (3, 2, 1, 1)])
def test_region_capture_encodes_exact_requested_pixels_as_png(frames, region):
    x, y, width, height = region
    screenshot = frames.display.capture_region(*region)
    assert (screenshot.width, screenshot.height) == (width, height)
    assert screenshot.frame_number == 1 and screenshot.timestamp > 0
    with screenshot.to_pil() as image:
        assert image.format == "PNG" and image.size == (width, height)
        assert image.tobytes() == bytes(
            value
            for row in range(y, y + height)
            for col in range(x, x + width)
            for value in (30 * col, 60 * row, 17)
        )
    assert frames.sent == [struct.pack("!BBHHHH", 3, 0, 0, 0, 4, 3)]


def test_raw_capture_preserves_server_pixels_and_updates_pointer_bounds(frames):
    screenshot = frames.display.capture_raw()
    assert (screenshot.width, screenshot.height) == (4, 3)
    with screenshot.to_pil() as image:
        assert image.getpixel((2, 1)) == (60, 60, 17)
    frames.transport.clear()
    frames.display.mouse_move(100, 100)
    assert events(frames) == [("pointer", 0, 3, 2)]


def test_region_capture_does_not_replace_full_screen_dimensions_or_break_the_next_capture(frames):
    first = frames.display.capture_raw()
    region = frames.display.capture_region(1, 1, 2, 1)
    following = frames.display.capture_raw()
    assert (first.frame_number, region.frame_number, following.frame_number) == (1, 2, 3)
    assert (following.width, following.height) == (4, 3)
    frames.transport.clear()
    frames.display.mouse_move(100, 100)
    assert events(frames) == [("pointer", 0, 3, 2)]


@pytest.mark.parametrize("previous_frame", [False, True])
def test_an_image_rejected_by_the_decoder_cannot_be_returned_with_guessed_dimensions(
    frames, monkeypatch, previous_frame
):
    if previous_frame:
        frames.display.capture_raw()
    else:
        frames.display._width = frames.display._height = 0
    # Exercise Pillow's documented decoder policy with a tiny valid RFB image.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 5)
    with pytest.raises(RuntimeError, match="画像"):
        frames.display.capture_raw()


def test_invalid_clipboard_reply_after_the_paste_gesture_closes_the_display(frames):
    negotiate(frames.protocol)
    proxy = frames.display._client
    write = frames.transport.write

    def invalid_reply(data):
        write(data)
        if data[0] == 4 and struct.unpack("!BBHI", data)[1:] == (1, 0, 0xFF63):
            frames.protocol.dataReceived(struct.pack("!Bxxxi", 3, -1))

    frames.transport.write = invalid_reply
    with pytest.raises(RuntimeError, match="クリップボード応答が不正"):
        frames.display.type_text("日本語")
    assert not frames.display.is_connected and proxy.closed
    assert frames.protocol.clipboard_provided.is_set()
    keys = [struct.unpack("!BBHI", data)[1:] for data in frames.sent if data[0] == 4]
    assert keys == [(1, 0, 0xFFE1), (1, 0, 0xFF63), (0, 0, 0xFF63), (0, 0, 0xFFE1)]


@pytest.mark.parametrize("phase", ["capabilities", "provide"])
def test_clipboard_timeout_closes_the_connection_before_another_action_can_be_sent(frames, phase):
    if phase == "provide":
        negotiate(frames.protocol)
    proxy = frames.display._client
    with pytest.raises(RuntimeError, match="貼り付け応答がありません"):
        frames.display.type_text("before 日本語")
    assert proxy.closed and not frames.display.is_connected
    before = list(frames.sent)
    with pytest.raises(RuntimeError, match="接続されていません"):
        frames.display.type_text("late command\n")
    assert frames.sent == before


def test_pending_clipboard_text_is_discarded_even_if_transport_abort_fails(frames, monkeypatch):
    negotiate(frames.protocol)
    frames.protocol.announce_text("pending private text")
    proxy = frames.display._client
    monkeypatch.setattr(
        frames.transport, "abortConnection", Mock(side_effect=OSError("injected abort failure"))
    )
    frames.display.disconnect()
    assert proxy.closed and frames.protocol._clipboard_payload is None
    assert frames.protocol.clipboard_ready.is_set() and frames.protocol.clipboard_provided.is_set()
    before = list(frames.sent)
    frames.protocol.dataReceived(server_message(REQUEST | TEXT))
    assert frames.sent == before


@pytest.mark.parametrize("failure", ["key", "tap"])
def test_android_wake_transport_failure_does_not_leave_a_pressed_key_or_button(wire, failure):
    write, failed = wire.transport.write, False

    def fail_once(data):
        nonlocal failed
        write(data)
        target = (
            data[0] == 4 and struct.unpack("!BBHI", data)[1:] == (1, 0, 0xFF1B)
            if failure == "key"
            else data[0] == 5 and data[1] == 1
        )
        if target and not failed:
            failed = True
            raise OSError("injected lost acknowledgement")

    wire.transport.write = fail_once
    wire.display.wake_android()
    packets = events(wire)
    assert failed and wire.protocol.buttons == 0
    assert packets[:4] == [
        ("key", 1, 0xFF1B),
        ("key", 0, 0xFF1B),
        ("key", 1, 0xFF67),
        ("key", 0, 0xFF67),
    ]
    assert packets[-1] == ("pointer", 0, 50, 40)


async def test_clipboard_timeout_stops_recovery_before_another_model_decision_or_input(
    frames, tmp_path, monkeypatch, session_retry_sleep
):
    negotiate(frames.protocol)
    model = MockLLMProvider()
    model.decide_next_action = AsyncMock(
        return_value=ActionDecision(
            Action(ActionType.TYPE, {"text": "before 日本語"}), "type report", 1, "type"
        )
    )
    session = TaskSession(
        llm=model,
        display=frames.display,
        store=TaskStore(tmp_path),
        decision=None,
        limits=ExecutionLimits(max_seconds=5),
    )
    assert await session.run("enter the report") is False
    assert model.decide_next_action.await_count == 1
    record = TaskStore(tmp_path).load(session.id)
    assert record.state == "failed" and record.stop_reason == "execution_error"
    assert len(record.actions) == 1 and record.actions[0].success is False
    assert "貼り付け応答がありません" in record.actions[0].error_message
    assert not frames.display.is_connected
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        detail = await client.get(f"/tasks/{session.id}")
    assert detail.status_code == 200 and detail.json()["state"] == "failed"
