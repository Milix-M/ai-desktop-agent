"""Gestures preserve bounds and balanced input in actual vncdotool RFB packets."""

import struct
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from twisted.internet.testing import StringTransport

from ai_desktop_agent.vm import vnc_transport
from ai_desktop_agent.vm.vnc_client import VNCClient


@pytest.fixture
def wire(monkeypatch):
    monkeypatch.setattr(
        vnc_transport, "reactor", SimpleNamespace(callFromThread=lambda fn, *args: fn(*args))
    )
    delays = []
    monkeypatch.setattr("ai_desktop_agent.vm.vnc_client.time.sleep", delays.append)
    factory = vnc_transport.ManagedVNCFactory()
    proxy = vnc_transport.ManagedVNCProxy(factory, timeout=0.2)
    protocol = vnc_transport.ManagedVNCProtocol()
    protocol.factory = factory
    protocol.transport = StringTransport()
    proxy.transport_connected(protocol)
    factory.clientConnectionMade(protocol)
    display = VNCClient()
    display._client, display._connected = proxy, True
    display._width, display._height = 100, 80
    yield SimpleNamespace(
        display=display, protocol=protocol, transport=protocol.transport, delays=delays
    )
    display.disconnect()


def events(wire):
    raw = wire.transport.value()
    result = []
    while raw:
        kind = raw[0]
        assert kind in {4, 5}
        length = 8 if kind == 4 else 6
        assert len(raw) >= length
        if kind == 5:
            _, buttons, x, y = struct.unpack("!BBHH", raw[:length])
            result.append(("pointer", buttons, x, y))
        else:
            _, down, _, key = struct.unpack("!BBHI", raw[:length])
            result.append(("key", down, key))
        raw = raw[length:]
    return result


@pytest.mark.parametrize(
    "start,end", [((-20, 120), (200, -10)), ((5, 5), (6, 6)), ((5, 5), (5, 5))]
)
def test_drag_keeps_button_held_during_motion_and_releases_at_the_bounded_destination(
    wire, start, end
):
    wire.display.mouse_drag(*start, *end)
    packets = events(wire)
    destination = (min(99, max(0, end[0])), min(79, max(0, end[1])))
    assert packets[0][1] == 0 and packets[-1] == ("pointer", 0, *destination)
    assert all(p[1] == 1 for p in packets[1:-1])
    assert all(0 <= p[2] < 100 and 0 <= p[3] < 80 for p in packets)
    assert wire.display.cursor_position == destination
    assert wire.protocol.buttons == 0


@pytest.mark.parametrize("position", [(200, -20), (None, None)])
def test_double_click_sends_two_complete_clicks_at_one_position(wire, position):
    wire.display.mouse_move(20, 30)
    wire.transport.clear()
    wire.display.mouse_double_click(*position)
    packets = events(wire)
    pressed = [p for p in packets if p[1] == 1]
    released = [p for p in packets if p[1] == 0]
    expected = (99, 0) if position[0] is not None else (20, 30)
    assert pressed == [("pointer", 1, *expected)] * 2
    assert len(released) >= 2 and packets[-1] == ("pointer", 0, *expected)
    assert all(p[2:] == expected for p in packets)
    assert wire.protocol.buttons == 0


@pytest.mark.parametrize("direction,mask", [("up", 8), ("down", 16)])
def test_scroll_releases_every_wheel_pulse_without_moving_the_pointer(wire, direction, mask):
    wire.display.mouse_move(20, 30)
    wire.transport.clear()
    wire.display.mouse_scroll(direction, 3)
    assert events(wire) == [("pointer", mask, 20, 30), ("pointer", 0, 20, 30)] * 3
    assert wire.protocol.buttons == 0


def test_separate_modifier_press_and_release_use_the_same_keysym(wire):
    wire.display.key_down("control")
    wire.display.key_up("ctrl")
    assert events(wire) == [("key", 1, 0xFFE3), ("key", 0, 0xFFE3)]


@pytest.mark.parametrize("dimensions,center", [((100, 80), (50, 40)), ((0, 0), (512, 384))])
def test_android_wake_releases_both_long_press_keys_before_the_center_tap(wire, dimensions, center):
    wire.display._width, wire.display._height = dimensions
    wire.display.wake_android()
    packets = events(wire)
    assert packets[:4] == [
        ("key", 1, 0xFF1B),
        ("key", 0, 0xFF1B),
        ("key", 1, 0xFF67),
        ("key", 0, 0xFF67),
    ]
    assert packets[-2:] == [("pointer", 1, *center), ("pointer", 0, *center)]
    assert sum(delay >= 1 for delay in wire.delays) == 2
    assert wire.protocol.buttons == 0


def test_disconnected_cursor_properties_preserve_the_last_position_without_raising(wire):
    wire.display.mouse_move(20, 30)
    wire.display.disconnect()
    assert wire.display.cursor_position == (20, 30)
    assert wire.display.cursor_x == 20 and wire.display.cursor_y == 30
    assert wire.display.has_custom_cursor is False
    assert not wire.display.is_connected


def test_disconnect_failure_still_makes_the_display_unavailable(wire, monkeypatch):
    monkeypatch.setattr(
        wire.transport, "abortConnection", Mock(side_effect=OSError("connection lost"))
    )
    wire.display.disconnect()
    assert not wire.display.is_connected
    with pytest.raises(RuntimeError, match="接続されていません"):
        wire.display.mouse_move(10, 10)


@pytest.mark.parametrize("interruption", ["stop", "transport"])
def test_interrupted_drag_releases_the_button_and_does_not_reach_the_destination(
    wire, interruption
):
    original = wire.transport.write
    interrupted = False

    def write(data):
        nonlocal interrupted
        original(data)
        if data[0] == 5:
            _, mask, x, y = struct.unpack("!BBHH", data)
            if mask == 1 and x > 5 and not interrupted:
                interrupted = True
                if interruption == "stop":
                    wire.display.request_stop()
                else:
                    raise OSError("movement acknowledgement lost")

    wire.transport.write = write
    with pytest.raises((RuntimeError, OSError)):
        wire.display.mouse_drag(5, 5, 90, 70)
    packets = events(wire)
    assert interrupted and packets[-1][1] == 0
    assert packets[-1][2:] != (90, 70)
    assert wire.protocol.buttons == 0
    assert wire.display.cursor_position == packets[-1][2:]
