import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image
from twisted.internet.defer import Deferred
from twisted.internet.error import ConnectionLost
from twisted.python.failure import Failure

from ai_desktop_agent.vm import vnc_transport
from ai_desktop_agent.vm.vnc_client import VNCClient


class TestProtocol(vnc_transport.ManagedVNCProtocol):
    __test__ = False

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.values = []

    def invalid(self):
        raise ValueError("invalid input")

    def nothing(self):
        return None

    def echo(self, value):
        self.values.append(value)
        return value

    def delayed(self):
        self.deferred = Deferred()
        self.entered.set()
        return self.deferred

    def captureScreen(self, output, format="png"):  # noqa: N802, A002
        Image.new("RGB", (32, 32), "white").save(output, format=format)
        return self

    def keyPress(self, key):  # noqa: N802
        self.values.append(key)
        self.factory.clientConnectionLost(None, Failure(ConnectionLost("server disconnected")))
        return self


@pytest.fixture
def connected(monkeypatch):
    monkeypatch.setattr(
        vnc_transport, "reactor", SimpleNamespace(callFromThread=lambda fn, *args: fn(*args))
    )
    factory = vnc_transport.ManagedVNCFactory()
    factory.protocol = TestProtocol
    proxy = vnc_transport.ManagedVNCProxy(factory, timeout=0.1)
    protocol = TestProtocol()
    protocol.factory = factory
    protocol.transport = SimpleNamespace(abortConnection=Mock(), write=Mock())
    proxy.transport_connected(protocol)
    factory.clientConnectionMade(protocol)
    yield proxy, protocol
    proxy.disconnect()


def test_protocol_attributes_follow_the_pointer_position_sent_on_the_wire(connected):
    proxy, protocol = connected
    assert (proxy.x, proxy.y) == (0, 0)
    proxy.mouseMove(17, 23)
    assert (proxy.x, proxy.y) == (17, 23)
    protocol.transport.write.assert_called_once_with(struct.pack("!BBHH", 5, 0, 17, 23))


def test_a_queued_calls_shorter_timeout_aborts_the_inflight_call_without_sending_it(connected):
    proxy, protocol = connected
    proxy.timeout = 3
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(proxy.delayed)
        assert protocol.entered.wait(1)
        proxy.timeout = 0.05
        queued = workers.submit(proxy.echo, "must not be sent")
        with pytest.raises(TimeoutError, match="VNC操作の応答待ち"):
            queued.result(timeout=1)
        with pytest.raises(TimeoutError, match="VNC操作の応答待ち"):
            first.result(timeout=1)
    assert proxy.closed and protocol.deferred is None
    assert protocol.values == []
    protocol.transport.abortConnection.assert_called_once()


def test_an_operation_error_does_not_poison_the_next_call_or_prevent_disconnect(connected):
    proxy, protocol = connected
    with pytest.raises(ValueError, match="invalid input"):
        proxy.invalid()
    assert proxy.echo("next operation") == "next operation"
    assert proxy.nothing() is None
    assert proxy.echo("after None") == "after None"
    proxy.disconnect()
    assert proxy.closed
    protocol.transport.abortConnection.assert_called_once()


def test_timeout_cancels_pending_work_and_rejects_later_calls(connected):
    proxy, protocol = connected
    with pytest.raises(TimeoutError):
        proxy.delayed()
    assert proxy.closed and protocol.deferred is None
    protocol.transport.abortConnection.assert_called_once()
    with pytest.raises(ConnectionError):
        proxy.echo("must not be sent")
    assert not protocol.values


def test_connection_loss_wakes_a_caller_instead_of_waiting_for_the_timeout(connected):
    proxy, protocol = connected
    proxy.timeout = 3
    with ThreadPoolExecutor(max_workers=1) as pool:
        call = pool.submit(proxy.delayed)
        assert protocol.entered.wait(1)
        protocol.connectionLost(Failure(ConnectionLost("server disconnected")))
        with pytest.raises(ConnectionLost):
            call.result(timeout=0.5)
    assert proxy.closed and protocol.deferred is None
    assert not protocol.clipboard_supported and protocol.clipboard_ready.is_set()


def test_concurrent_callers_get_their_own_results_in_order(connected):
    proxy, protocol = connected
    proxy.timeout = 3
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(proxy.delayed)
        assert protocol.entered.wait(1)
        second = pool.submit(proxy.echo, "second result")
        assert not protocol.values
        pending, protocol.deferred = protocol.deferred, None
        pending.callback("first result")
        assert first.result(timeout=1) == "first result"
        assert second.result(timeout=1) == "second result"


def test_disconnect_prevents_a_queued_operation_from_reaching_the_server(connected, monkeypatch):
    proxy, protocol = connected
    proxy.timeout = 3
    queued, entered = [], threading.Event()

    def schedule(fn, *args):
        queued.append((fn, args))
        entered.set()

    monkeypatch.setattr(vnc_transport, "reactor", SimpleNamespace(callFromThread=schedule))
    with ThreadPoolExecutor(max_workers=1) as pool:
        call = pool.submit(proxy.echo, "must not be sent")
        assert entered.wait(1)
        proxy.disconnect()
        with pytest.raises(ConnectionError):
            call.result(timeout=0.5)
    for fn, args in queued:
        fn(*args)
    assert not protocol.values
    protocol.transport.abortConnection.assert_called_once()


def test_connect_timeout_cancels_the_endpoint_and_closes_a_late_transport(monkeypatch):
    monkeypatch.setattr(
        vnc_transport, "reactor", SimpleNamespace(callFromThread=lambda fn, *args: fn(*args))
    )
    cancelled = Mock()
    attempt = Deferred(lambda _: cancelled())
    endpoint = Mock()
    endpoint.connect.return_value = attempt
    monkeypatch.setattr(
        vnc_transport, "ContainerHostnameEndpoint", lambda *args, **kwargs: endpoint
    )
    factory = vnc_transport.ManagedVNCFactory()
    proxy = vnc_transport.ManagedVNCProxy(factory, timeout=0.01)
    proxy.connect("127.0.0.1")
    with pytest.raises(TimeoutError):
        proxy.connection_ready()
    assert proxy.closed
    cancelled.assert_called_once()
    transport = SimpleNamespace(abortConnection=Mock())
    late = vnc_transport.ManagedVNCProtocol()
    late.transport = transport
    proxy.transport_connected(late)
    transport.abortConnection.assert_called_once()


def test_vnc_client_reconnect_closes_the_old_proxy_and_resets_stop_and_coordinates(monkeypatch):
    from vncdotool import api

    old, new = Mock(closed=False), Mock(closed=False)
    connector = Mock(return_value=new)
    monkeypatch.setattr(api, "connect", connector)
    display = VNCClient()
    display._client, display._connected = old, True
    display._width, display._height = 100, 100
    display._cursor_x, display._cursor_y = 50, 50
    display.request_stop()
    display.connect("127.0.0.1", 5900)
    old.disconnect.assert_called_once()
    new.connection_ready.assert_called_once()
    assert display.is_connected and not display._stop_requested.is_set()
    assert (display._width, display._height, display._cursor_x, display._cursor_y) == (0, 0, 0, 0)
    new.closed = True
    assert not display.is_connected
    with pytest.raises(RuntimeError, match="接続されていません"):
        display.mouse_move(1, 1)


def test_endpoint_setup_failure_is_reported_without_waiting_for_a_timeout(monkeypatch):
    monkeypatch.setattr(
        vnc_transport, "reactor", SimpleNamespace(callFromThread=lambda fn, *args: fn(*args))
    )
    endpoint = Mock(side_effect=ValueError("invalid endpoint"))
    monkeypatch.setattr(vnc_transport, "ContainerHostnameEndpoint", endpoint)
    proxy = vnc_transport.ManagedVNCProxy(vnc_transport.ManagedVNCFactory(), timeout=0.1)
    proxy.connect("invalid")
    assert proxy.closed
    assert isinstance(proxy._ready.exception(), ValueError)
    with pytest.raises(ConnectionError):
        proxy.connection_ready()


async def test_connection_loss_during_input_stops_the_task_and_preserves_the_partial_action(
    connected, tmp_path
):
    from ai_desktop_agent.actions.primitives import Action, ActionType
    from ai_desktop_agent.agent.llm.mock import MockLLMProvider
    from ai_desktop_agent.agent.llm.types import ActionDecision
    from ai_desktop_agent.server.session import TaskSession
    from ai_desktop_agent.server.store import TaskStore

    proxy, protocol = connected
    display = VNCClient()
    display._client, display._connected = proxy, True
    model = MockLLMProvider()
    model.decide_next_action = AsyncMock(
        return_value=ActionDecision(Action(ActionType.TYPE, {"text": "abc"}), "入力", 1, "入力する")
    )
    store = TaskStore(tmp_path)
    session = TaskSession(llm=model, display=display, store=store, decision=None)
    assert not await session.run("テキストを入力する")
    assert protocol.values == ["a"]
    assert not display.is_connected and proxy.closed
    record = store.load(session.id)
    assert record.state == "failed" and len(record.actions) == 1
    assert (
        not record.actions[0].success and "server disconnected" in record.actions[0].error_message
    )
    assert model.decide_next_action.call_count == 1


@pytest.mark.parametrize("cancelled", [False, True])
def test_endpoint_failure_after_a_cancelled_or_pending_handshake_cannot_send_input(
    monkeypatch, cancelled
):
    monkeypatch.setattr(
        vnc_transport, "reactor", SimpleNamespace(callFromThread=lambda fn, *args: fn(*args))
    )
    factory = vnc_transport.ManagedVNCFactory()
    factory.protocol = TestProtocol
    proxy = vnc_transport.ManagedVNCProxy(factory, timeout=3)
    handshake_failures = []
    # Twisted cancellation and a later dial failure can notify the same factory.
    factory.deferred.addErrback(lambda failure: handshake_failures.append(failure.value))
    if cancelled:
        factory.deferred.cancel()
        assert proxy.closed
    factory.clientConnectionFailed(None, Failure(ConnectionLost("endpoint disconnected")))
    assert proxy.closed
    with pytest.raises(ConnectionError, match="切断"):
        proxy.echo("must not reach desktop")
    # The proxy consumes failures rather than leaving an unhandled Twisted Failure.
    assert handshake_failures == []
    proxy.disconnect()


def test_disconnect_before_scheduled_connect_never_opens_a_transport(monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        vnc_transport,
        "reactor",
        SimpleNamespace(callFromThread=lambda fn, *args: scheduled.append((fn, args))),
    )
    endpoint = Mock(side_effect=AssertionError("closed proxy must not connect"))
    monkeypatch.setattr(vnc_transport, "ContainerHostnameEndpoint", endpoint)
    proxy = vnc_transport.ManagedVNCProxy(vnc_transport.ManagedVNCFactory(), timeout=3)
    proxy.connect("desktop_service")
    proxy.disconnect()
    for fn, args in scheduled:
        fn(*args)
    endpoint.assert_not_called()
    assert proxy.closed
    with pytest.raises(ConnectionError, match="切断"):
        proxy.connection_ready()


def test_late_and_duplicate_handshake_cannot_reopen_a_disconnected_proxy(monkeypatch):
    queued = []
    monkeypatch.setattr(
        vnc_transport,
        "reactor",
        SimpleNamespace(callFromThread=lambda fn, *args: queued.append((fn, args))),
    )
    factory = vnc_transport.ManagedVNCFactory()
    factory.protocol = TestProtocol
    proxy = vnc_transport.ManagedVNCProxy(factory, timeout=3)
    protocol = TestProtocol()
    protocol.factory = factory
    protocol.transport = SimpleNamespace(abortConnection=Mock(), write=Mock())
    proxy.transport_connected(protocol)
    proxy.disconnect()
    # A handshake already being processed can finish before the reactor drains
    # the transport-close request posted by another thread.
    factory.clientConnectionMade(protocol)
    factory.clientConnectionMade(protocol)
    for fn, args in queued:
        fn(*args)
    assert proxy.closed
    with pytest.raises(ConnectionError, match="切断"):
        proxy.echo("must not reach desktop")
    assert protocol.values == []
    protocol.transport.abortConnection.assert_called_once()


def test_disconnect_after_readiness_before_dispatch_never_sends_the_waiting_operation(monkeypatch):
    from concurrent.futures import Future

    returned, release = threading.Event(), threading.Event()

    class ReadinessGate(Future):
        def result(self, timeout=None):
            value = super().result(timeout)
            if isinstance(value, TestProtocol):
                returned.set()
                assert release.wait(3)
            return value

    # Preserve real Future results/errors while pausing the calling thread at
    # the readiness handoff; no proxy fields or session state are modified.
    monkeypatch.setattr(vnc_transport, "Future", ReadinessGate)
    monkeypatch.setattr(
        vnc_transport, "reactor", SimpleNamespace(callFromThread=lambda fn, *args: fn(*args))
    )
    factory = vnc_transport.ManagedVNCFactory()
    factory.protocol = TestProtocol
    proxy = vnc_transport.ManagedVNCProxy(factory, timeout=3)
    protocol = TestProtocol()
    protocol.factory = factory
    protocol.transport = SimpleNamespace(abortConnection=Mock(), write=Mock())
    proxy.transport_connected(protocol)
    factory.clientConnectionMade(protocol)
    with ThreadPoolExecutor(max_workers=1) as workers:
        call = workers.submit(proxy.echo, "must not reach desktop")
        try:
            assert returned.wait(1)
            proxy.disconnect()
        finally:
            release.set()
        with pytest.raises(ConnectionError, match="切断"):
            call.result(timeout=1)
    assert proxy.closed and protocol.values == []
    protocol.transport.abortConnection.assert_called_once()
