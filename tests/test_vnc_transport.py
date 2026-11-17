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
    protocol.transport = SimpleNamespace(abortConnection=Mock())
    proxy.transport_connected(protocol)
    factory.clientConnectionMade(protocol)
    yield proxy, protocol
    proxy.disconnect()


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
    monkeypatch.setattr(vnc_transport, "HostnameEndpoint", lambda *args, **kwargs: endpoint)
    factory = vnc_transport.ManagedVNCFactory()
    proxy = vnc_transport.ManagedVNCProxy(factory, timeout=0.01)
    proxy.connect("127.0.0.1")
    with pytest.raises(TimeoutError):
        proxy.connection_ready()
    assert proxy.closed
    cancelled.assert_called_once()
    transport = SimpleNamespace(abortConnection=Mock())
    proxy.transport_connected(SimpleNamespace(transport=transport, deferred=None))
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
    monkeypatch.setattr(vnc_transport, "HostnameEndpoint", endpoint)
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
