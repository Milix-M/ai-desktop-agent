import threading
import zlib
from struct import pack, unpack
from unittest.mock import Mock

import pytest

from ai_desktop_agent.vm.clipboard import (
    CAPS,
    MAX_MESSAGE_BYTES,
    MAX_TEXT_BYTES,
    NOTIFY,
    PEEK,
    PROVIDE,
    REQUEST,
    TEXT,
    ClipboardClient,
)
from ai_desktop_agent.vm.vnc_client import VNCClient


def server_message(flags, payload=b""):
    body = pack("!I", flags) + payload
    return pack("!Bxxxi", 3, -len(body)) + body


@pytest.fixture
def protocol():
    client = ClipboardClient()
    client.transport = Mock()
    client.factory = Mock()
    client._handler = client._handleExpected
    client.expect(client._handleConnection, 1)
    return client


def negotiate(protocol):
    protocol.dataReceived(server_message(CAPS | TEXT | NOTIFY | PROVIDE, pack("!I", 0)))


def test_fragmented_capabilities_and_utf8_transfer_follow_the_wire_format(protocol):
    packet = server_message(CAPS | TEXT | NOTIFY | PROVIDE, pack("!I", 0))
    for byte in packet:
        protocol.dataReceived(bytes([byte]))
    assert protocol.clipboard_ready.is_set() and protocol.clipboard_supported
    assert protocol.connection_ready() is protocol
    assert protocol.announce_text("日本語 👩‍💻\nnext\tline") is protocol
    assert not protocol.clipboard_provided.is_set()
    protocol.dataReceived(server_message(REQUEST | TEXT))
    wire = protocol.transport.write.call_args.args[0]
    kind, length, flags = unpack("!BxxxiI", wire[:12])
    assert kind == 6 and -length == len(wire) - 8 and flags == PROVIDE | TEXT
    data = zlib.decompress(wire[12:])
    text = "日本語 👩‍💻\r\nnext\tline".encode() + b"\0"
    assert data == pack("!I", len(text)) + text
    assert protocol.clipboard_provided.is_set()
    # A repeated paste can request the same text again without corrupting the proxy.
    protocol.dataReceived(server_message(REQUEST | TEXT))
    assert protocol.transport.write.call_args.args[0] == wire


def test_new_text_replaces_old_clipboard_data(protocol):
    negotiate(protocol)
    protocol.announce_text("前の内容")
    protocol.dataReceived(server_message(REQUEST | TEXT))
    protocol.announce_text("次の内容")
    assert not protocol.clipboard_provided.is_set()
    protocol.dataReceived(server_message(REQUEST | TEXT))
    assert "次の内容".encode() in zlib.decompress(protocol.transport.write.call_args.args[0][12:])


def test_desktop_clipboard_contents_are_never_requested_decoded_or_echoed(protocol):
    negotiate(protocol)
    protocol.copy_text = Mock()
    protocol.bell = Mock()
    protocol.transport.reset_mock()
    # Even unsolicited compressed data is skipped without decompressing it.
    protocol.dataReceived(server_message(PROVIDE | TEXT, b"not a zlib stream"))
    protocol.dataReceived(server_message(NOTIFY | TEXT))
    secret = b"private desktop clipboard"
    protocol.dataReceived(pack("!BxxxI", 3, len(secret)) + secret)
    protocol.dataReceived(b"\2")
    protocol.copy_text.assert_not_called()
    protocol.transport.write.assert_not_called()
    protocol.bell.assert_called_once()


def test_peek_only_announces_the_available_format(protocol):
    negotiate(protocol)
    protocol.dataReceived(server_message(PEEK))
    assert protocol.transport.write.call_args.args[0] == pack("!BxxxiI", 6, -4, NOTIFY)
    protocol.announce_text("日本語")
    protocol.dataReceived(server_message(PEEK))
    assert protocol.transport.write.call_args.args[0] == pack("!BxxxiI", 6, -4, NOTIFY | TEXT)


@pytest.mark.parametrize("length", [-1, -3, -MAX_MESSAGE_BYTES - 1, 2**31 - 1, -(2**31)])
def test_untrusted_lengths_cannot_allocate_unbounded_memory(protocol, length):
    protocol.dataReceived(pack("!Bxxxi", 3, length))
    protocol.transport.loseConnection.assert_called_once()
    assert protocol.clipboard_error and protocol.clipboard_ready.is_set()


def test_incomplete_capability_sizes_close_the_connection(protocol):
    protocol.dataReceived(server_message(CAPS | TEXT))
    protocol.transport.loseConnection.assert_called_once()
    assert not protocol.clipboard_supported


def test_disconnect_wakes_both_waiters_and_discards_pending_text(protocol):
    negotiate(protocol)
    protocol.announce_text("日本語")
    protocol.connectionLost(None)
    assert protocol.clipboard_ready.is_set() and protocol.clipboard_provided.is_set()
    assert protocol.clipboard_error and protocol._clipboard_payload is None


@pytest.mark.parametrize(
    "text", ["before\0日本語", "before\x1b日本語", "before\ud800", "日" * MAX_TEXT_BYTES]
)
def test_invalid_or_oversized_input_does_not_send_a_partial_prefix(protocol, text):
    negotiate(protocol)
    display = VNCClient()
    display._connected = True
    display._client = Mock(protocol=protocol)
    with pytest.raises(ValueError):
        display.type_text(text)
    assert not display._client.mock_calls


def test_a_server_without_text_capabilities_does_not_receive_a_partial_prefix(protocol):
    protocol.dataReceived(server_message(CAPS))
    display = VNCClient()
    display._connected = True
    display._client = Mock(protocol=protocol)
    with pytest.raises(ValueError, match="Unicode"):
        display.type_text("before 日本語")
    assert not display._client.mock_calls


async def test_unicode_wait_is_cooperatively_cancelled_without_typing(protocol):
    import asyncio

    display = VNCClient()
    display._connected = True
    display._client = Mock(protocol=protocol)
    entered = threading.Event()
    original_wait = protocol.clipboard_ready.wait

    def wait(timeout):
        entered.set()
        return original_wait(timeout)

    protocol.clipboard_ready.wait = wait
    task = asyncio.create_task(asyncio.to_thread(display.type_text, "before 日本語"))
    assert await asyncio.to_thread(entered.wait, 1)
    display.request_stop()
    with pytest.raises(RuntimeError, match="停止"):
        await asyncio.wait_for(task, 1)
    assert not display._client.mock_calls
