"""Check framing, correlation, deadlines, and cleanup through real UNIX sockets."""

import json
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_desktop_agent.vm.qmp import QmpClient, QmpError


class Monitor:
    def __init__(
        self, handler, greeting=b'{"QMP":{"version":{},"capabilities":[]}}\r\n', greeting_delay=0
    ):
        self.handler = handler
        self.greeting = greeting
        self.greeting_delay = greeting_delay
        self.directory = tempfile.TemporaryDirectory(prefix="qmp-test-")
        self.path = str(Path(self.directory.name) / "monitor.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen()
        self.listener.settimeout(0.05)
        self.stopping = threading.Event()
        self.disconnected = threading.Event()
        self.requests = []
        self.errors = []
        self.connection = None
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def _serve(self):
        try:
            while not self.stopping.is_set():
                try:
                    connection, _ = self.listener.accept()
                except TimeoutError:
                    continue
                self.connection = connection
                self.disconnected.clear()
                try:
                    with connection, connection.makefile("rb") as stream:
                        self.stopping.wait(self.greeting_delay)
                        connection.sendall(self.greeting)
                        for line in stream:
                            request = json.loads(line)
                            self.requests.append(request)
                            response = self.handler(request, connection, self.stopping)
                            if response is not None:
                                if not isinstance(response, bytes):
                                    response = (json.dumps(response) + "\r\n").encode()
                                connection.sendall(response)
                except OSError:
                    pass  # Disconnecting during a deliberately bad/late response is expected.
                finally:
                    self.disconnected.set()
                    self.connection = None
        except Exception as error:
            self.errors.append(error)

    def __exit__(self, *_):
        self.stopping.set()
        connection = self.connection
        if connection is not None:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
        self.thread.join(timeout=2)
        self.listener.close()
        self.directory.cleanup()
        assert not self.thread.is_alive()
        assert not self.errors


def response(request, value=None):
    return {"id": request["id"], "return": {} if value is None else value}


def test_events_and_unknown_responses_do_not_replace_the_requested_status():
    def handle(request, connection, _):
        if request["execute"] == "qmp_capabilities":
            return response(request)
        for message in (
            {"event": "STOP"},
            {"return": {"status": "stale"}},
            {"id": True, "return": {"status": "invalid-id"}},
            {"id": request["id"] + 1, "return": {"status": "unknown"}},
        ):
            connection.sendall((json.dumps(message) + "\n").encode())
        return response(request, {"status": "running", "running": True})

    with Monitor(handle) as monitor, QmpClient(monitor.path) as client:
        for _ in range(3):
            assert client.query_status() == {"status": "running", "running": True}
        assert [r["execute"] for r in monitor.requests] == [
            "qmp_capabilities",
            "query-status",
            "query-status",
            "query-status",
        ]
        assert len({r["id"] for r in monitor.requests}) == 4


def test_fragmented_messages_are_read_and_commands_are_serialized():
    def handle(request, connection, _):
        encoded = (json.dumps(response(request, request["arguments"])) + "\r\n").encode()
        for start in range(0, len(encoded), 3):
            connection.sendall(encoded[start : start + 3])
        return None

    def negotiate_then_handle(request, connection, stopping):
        if request["execute"] == "qmp_capabilities":
            return response(request)
        return handle(request, connection, stopping)

    with Monitor(negotiate_then_handle) as monitor, QmpClient(monitor.path) as client:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda i: client.execute("echo", {"value": i}), range(12)))
        assert [item["return"] for item in results] == [{"value": i} for i in range(12)]


@pytest.mark.parametrize("greeting", [b'{"other":"QMP"}\n', b"[]\n", b"\xff\n", b"broken\n"])
def test_failed_greeting_closes_the_peer_connection(greeting):
    with Monitor(lambda request, *_: response(request), greeting=greeting) as monitor:
        client = QmpClient(monitor.path, timeout=0.2)
        with pytest.raises(QmpError, match="接続に失敗"):
            client.connect()
        assert monitor.disconnected.wait(1)
        with pytest.raises(QmpError, match="未接続"):
            client.query_status()


def test_capability_failure_closes_connection_and_can_be_retried():
    reject = True

    def handle(request, *_):
        if reject:
            return {"id": request["id"], "error": {"class": "GenericError", "desc": "rejected"}}
        return response(request, {"status": "running"})

    with Monitor(handle) as monitor:
        client = QmpClient(monitor.path)
        with pytest.raises(QmpError, match="capabilities失敗"):
            client.connect()
        assert monitor.disconnected.wait(1)
        reject = False
        with client:
            assert client.query_status()["status"] == "running"
            client.connect()
            assert client.query_status()["status"] == "running"


def test_greeting_and_capabilities_share_one_connection_deadline():
    def handle(request, _, stopping):
        stopping.wait(0.08)
        return response(request)

    with Monitor(handle, greeting_delay=0.06) as monitor:
        client = QmpClient(monitor.path, timeout=0.1)
        with pytest.raises(QmpError, match="接続に失敗"):
            client.connect()
        assert monitor.disconnected.wait(1)
        with pytest.raises(QmpError, match="未接続"):
            client.execute("query-status")


def test_partial_response_then_disconnect_can_be_reconnected():
    disconnect = True

    def handle(request, connection, _):
        if request["execute"] == "qmp_capabilities":
            return response(request)
        if disconnect:
            connection.sendall(b'{"return":')
            connection.shutdown(socket.SHUT_RDWR)
            return None
        return response(request, {"status": "running"})

    with Monitor(handle) as monitor, QmpClient(monitor.path) as client:
        with pytest.raises(QmpError, match="切断"):
            client.query_status()
        assert monitor.disconnected.wait(1)
        disconnect = False
        client.connect()
        assert client.query_status()["status"] == "running"


@pytest.mark.parametrize("status", [None, [], {}, {"status": True}])
def test_invalid_status_is_rejected_and_closes_the_connection(status):
    def handle(request, *_):
        return {"id": request["id"], "return": status}

    with Monitor(handle) as monitor, QmpClient(monitor.path) as client:
        with pytest.raises(QmpError, match="応答形式"):
            client.query_status()
        assert monitor.disconnected.wait(1)


@pytest.mark.parametrize("kind", ["silence", "events", "partial"])
def test_command_has_an_absolute_deadline_and_requires_reconnection(kind):
    def handle(request, connection, stopping):
        if request["execute"] == "qmp_capabilities":
            return response(request)
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline and not stopping.wait(0.01):
            if kind == "events":
                connection.sendall(b'{"event":"STOP"}\n')
            elif kind == "partial":
                connection.sendall(b" ")
        return response(request, {"status": "late"})

    with Monitor(handle) as monitor, QmpClient(monitor.path, timeout=0.1) as client:
        started = time.monotonic()
        with pytest.raises(QmpError):
            client.query_status()
        assert time.monotonic() - started < 1
        with pytest.raises(QmpError, match="未接続"):
            client.query_status()


@pytest.mark.parametrize(
    "bad", [b"[]\n", b"{broken}\n", b"\xff\n", b"[" * 1100 + b"]" * 1100 + b"\n"]
)
def test_invalid_json_closes_the_connection(bad):
    def handle(request, *_):
        return response(request) if request["execute"] == "qmp_capabilities" else bad

    with Monitor(handle) as monitor, QmpClient(monitor.path) as client:
        with pytest.raises(QmpError):
            client.query_status()
        assert monitor.disconnected.wait(1)
        with pytest.raises(QmpError, match="未接続"):
            client.query_status()


@pytest.mark.parametrize("terminated", [False, True])
def test_oversized_response_is_rejected(monkeypatch, terminated):
    monkeypatch.setattr("ai_desktop_agent.vm.qmp.MAX_MESSAGE_BYTES", 128)

    def handle(request, *_):
        if request["execute"] == "qmp_capabilities":
            return response(request)
        return b" " * 256 + (b"\n" if terminated else b"")

    with Monitor(handle) as monitor, QmpClient(monitor.path) as client:
        with pytest.raises(QmpError, match="サイズ上限"):
            client.query_status()
        assert monitor.disconnected.wait(1)


def test_error_response_does_not_poison_the_next_command():
    def handle(request, *_):
        if request["execute"] == "unknown":
            return {"id": request["id"], "error": {"class": "CommandNotFound", "desc": "unknown"}}
        return response(request, {"status": "paused"})

    with Monitor(handle) as monitor, QmpClient(monitor.path) as client:
        assert client.execute("unknown")["error"]["class"] == "CommandNotFound"
        assert client.query_status()["status"] == "paused"


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_timeout_must_be_finite_and_positive(timeout):
    with pytest.raises(ValueError):
        QmpClient("unused.sock", timeout=timeout)


def test_expired_command_deadline_closes_before_a_send(monkeypatch):
    from ai_desktop_agent.vm import qmp

    with Monitor(lambda request, *_: response(request)) as monitor:
        client = QmpClient(monitor.path, timeout=1)
        client.connect()
        ticks = iter([0.0, 2.0])
        # Model a thread being descheduled after setting the command deadline.
        monkeypatch.setattr(qmp, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
        with pytest.raises(QmpError, match="上限"):
            client.query_status()
        assert monitor.disconnected.wait(1)
        assert [request["execute"] for request in monitor.requests] == ["qmp_capabilities"]
        with pytest.raises(QmpError, match="未接続"):
            client.query_status()


def test_ambiguous_command_response_closes_the_connection():
    def handle(request, *_):
        result = response(request)
        if request["execute"] != "qmp_capabilities":
            result["error"] = {"class": "GenericError", "desc": "contradictory result"}
        return result

    with Monitor(handle) as monitor, QmpClient(monitor.path) as client:
        with pytest.raises(QmpError, match="returnまたはerror"):
            client.query_status()
        assert monitor.disconnected.wait(1)


def test_query_error_is_reported_without_losing_the_next_valid_status():
    queries = []

    def handle(request, *_):
        if request["execute"] == "query-status":
            queries.append(request)
            if len(queries) == 1:
                return {"id": request["id"], "error": {"class": "GenericError", "desc": "busy"}}
        return response(request, {"status": "running"})

    with Monitor(handle) as monitor, QmpClient(monitor.path) as client:
        with pytest.raises(QmpError, match="query-status失敗"):
            client.query_status()
        assert client.query_status() == {"status": "running"}
        assert len(queries) == 2
