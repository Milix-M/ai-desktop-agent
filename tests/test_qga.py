"""Exercise dirty streams, response ownership, deadlines, and bounded command output."""

import base64
import json
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

import pytest

from ai_desktop_agent.vm.qga import QgaClient, QgaError, QgaOutputLimitError, QgaTimeoutError


def reply(request, value):
    return {"id": request.get("id"), "return": value}


def encoded(value):
    return (json.dumps(value) + "\n").encode()


class Agent:
    def __init__(self, handle, *, stale=b"", sync=None):
        self.handle, self.stale, self.sync = handle, stale, sync
        self.directory = tempfile.TemporaryDirectory(prefix="qga-test-")
        self.path = str(Path(self.directory.name) / "agent.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen()
        self.listener.settimeout(0.05)
        self.stopping = threading.Event()
        self.disconnected = threading.Event()
        self.requests, self.raw, self.errors = [], [], []
        self.connection = None
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def serve(self):
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
                        connection.sendall(self.stale)
                        for line in stream:
                            self.raw.append(line)
                            request = json.loads(line.rsplit(b"\xff", 1)[-1])
                            self.requests.append(request)
                            if request["execute"] == "guest-sync-delimited":
                                response = (
                                    self.sync(request, connection, self.stopping)
                                    if self.sync
                                    else b"\xff"
                                    + encoded(reply(request, request["arguments"]["id"]))
                                )
                            else:
                                response = self.handle(request, connection, self.stopping)
                            if response is not None:
                                connection.sendall(
                                    response if isinstance(response, bytes) else encoded(response)
                                )
                except OSError:
                    pass
                finally:
                    self.connection = None
                    self.disconnected.set()
        except Exception as error:
            self.errors.append(error)

    def __exit__(self, *_):
        self.stopping.set()
        connection = self.connection
        if connection:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
        self.thread.join(timeout=2)
        self.listener.close()
        self.directory.cleanup()
        assert not self.thread.is_alive()
        assert not self.errors


@pytest.mark.parametrize(
    "stale",
    [
        b'{"id":2,"return":{"name":"old"}}\n',
        b"partial JSON from a previous client",
        b'\xff{"id":1,"return":-1}\n{"partial":',
    ],
)
def test_initial_synchronization_discards_previous_clients_data(stale):
    with Agent(lambda request, *_: reply(request, {"name": "current"}), stale=stale) as peer:
        with QgaClient(peer.path) as client:
            assert client.get_osinfo()["name"] == "current"
        assert peer.requests[0]["execute"] == "guest-sync-delimited"
        assert peer.raw[0].startswith(b"\xff")
        assert 0 <= peer.requests[0]["arguments"]["id"] < 2**63


def test_sync_ignores_wrong_nonces_and_resets_at_a_new_sentinel():
    def sync(request, connection, _):
        connection.sendall(b'\xff{"partial":')
        connection.sendall(b"\xff" + encoded(reply(request, -1)))
        return b"\xff" + encoded(reply(request, request["arguments"]["id"]))

    with (
        Agent(lambda request, *_: reply(request, {}), sync=sync) as peer,
        QgaClient(peer.path) as client,
    ):
        assert client.ping()


def test_unknown_response_ids_do_not_replace_current_results():
    def handle(request, connection, _):
        connection.sendall(encoded({"id": request["id"] + 1, "return": {"name": "stale"}}))
        return reply(request, {"name": "current"})

    with Agent(handle) as peer, QgaClient(peer.path) as client:
        for _ in range(3):
            assert client.get_osinfo() == {"name": "current"}


def test_fragmented_commands_are_serialized_and_errors_do_not_poison_following_calls():
    def handle(request, connection, _):
        if request["execute"] == "unknown":
            return {"id": request["id"], "error": {"class": "CommandNotFound", "desc": "unknown"}}
        data = encoded(reply(request, request.get("arguments", {})))
        for start in range(0, len(data), 2):
            connection.sendall(data[start : start + 2])
        return None

    with Agent(handle) as peer, QgaClient(peer.path) as client:
        with pytest.raises(QgaError):
            client.execute("unknown")
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda i: client.execute("echo", {"value": i}), range(8)))
        assert results == [{"value": i} for i in range(8)]


def test_failed_sync_closes_the_socket_and_reconnects_cleanly():
    def sync(request, *_):
        return b"\xff" + encoded(reply(request, -1))

    with Agent(lambda request, *_: reply(request, {}), sync=sync) as peer:
        client = QgaClient(peer.path, timeout=0.08)
        with pytest.raises(QgaError):
            client.connect()
        assert peer.disconnected.wait(1)
        peer.sync = None
        with client:
            assert client.ping()


@pytest.mark.parametrize("kind", ["silence", "partial", "unknown_ids"])
def test_response_waits_have_an_absolute_deadline_and_close_the_connection(kind):
    def handle(request, connection, stopping):
        deadline = time.monotonic() + 0.3
        while not stopping.wait(0.01) and time.monotonic() < deadline:
            if kind == "partial":
                connection.sendall(b" ")
            elif kind == "unknown_ids":
                connection.sendall(encoded({"id": -1, "return": {}}))
        return reply(request, {})

    with Agent(handle) as peer, QgaClient(peer.path, timeout=0.08) as client:
        started = time.monotonic()
        with pytest.raises(QgaTimeoutError):
            client.ping()
        assert time.monotonic() - started < 0.5
        with pytest.raises(QgaError, match="未接続"):
            client.ping()


@pytest.mark.parametrize(
    "data", [b"[]\n", b"{bad}\n", b"\xff\n", b"[" * 1100 + b"]" * 1100 + b"\n"]
)
def test_bad_json_closes_the_connection(data):
    with Agent(lambda *_: data) as peer, QgaClient(peer.path) as client:
        with pytest.raises(QgaError):
            client.ping()
        assert peer.disconnected.wait(1)


@pytest.mark.parametrize("newline", [b"", b"\n"])
def test_message_size_is_bounded(monkeypatch, newline):
    monkeypatch.setattr("ai_desktop_agent.vm.qga.MAX_MESSAGE_BYTES", 128)
    with Agent(lambda *_: b" " * 256 + newline) as peer, QgaClient(peer.path) as client:
        with pytest.raises(QgaError, match="サイズ上限"):
            client.ping()
        assert peer.disconnected.wait(1)


def test_oversized_request_does_not_send_or_break_the_connection(monkeypatch):
    monkeypatch.setattr("ai_desktop_agent.vm.qga.MAX_MESSAGE_BYTES", 128)
    with Agent(lambda request, *_: reply(request, {})) as peer, QgaClient(peer.path) as client:
        with pytest.raises(ValueError):
            client.execute("echo", {"text": "a" * 256})
        assert client.ping()
        assert [r["execute"] for r in peer.requests] == ["guest-sync-delimited", "guest-ping"]


@pytest.mark.parametrize("delay_at", ["guest-exec", "guest-exec-status"])
def test_run_deadline_includes_startup_and_status_responses(delay_at):
    def handle(request, _, stopping):
        if request["execute"] == delay_at:
            stopping.wait(0.2)
        return reply(
            request, {"pid": 42} if request["execute"] == "guest-exec" else {"exited": False}
        )

    with Agent(handle) as peer, QgaClient(peer.path, timeout=0.5) as client:
        with pytest.raises(QgaTimeoutError) as failure:
            client.run(["example", "private-input-must-not-be-in-the-error"], timeout=0.05)
        assert failure.value.pid == (None if delay_at == "guest-exec" else 42)
        assert "private-input" not in str(failure.value)


@pytest.mark.parametrize(
    "status",
    [
        [],
        {"exited": "false"},
        {"exited": True, "exitcode": 0, "out-data": "%%%"},
        {"exited": True, "exitcode": 0, "err-data": 7},
        {"exited": True},
        {"exited": True, "exitcode": 0, "out-truncated": "false"},
    ],
)
def test_invalid_execution_results_are_not_accepted(status):
    def handle(request, *_):
        return reply(request, {"pid": 42} if request["execute"] == "guest-exec" else status)

    with Agent(handle) as peer, QgaClient(peer.path) as client:
        with pytest.raises(QgaError) as failure:
            client.run(["example"])
        assert failure.value.pid == 42
        assert peer.disconnected.wait(1)


@pytest.mark.parametrize("agent_truncated", [False, True])
def test_output_limits_retain_a_bounded_prefix_without_claiming_complete_output(agent_truncated):
    raw = b"prefix" if agent_truncated else ("日本語" * 3000).encode()
    status = {"exited": True, "exitcode": 0, "out-data": base64.b64encode(raw).decode()}
    if agent_truncated:
        status["out-truncated"] = True

    def handle(request, *_):
        if request["execute"] == "guest-exec":
            return reply(request, {"pid": 42})
        return reply(request, status if request["execute"] == "guest-exec-status" else {})

    with Agent(handle) as peer, QgaClient(peer.path) as client:
        with pytest.raises(QgaOutputLimitError) as failure:
            client.run(["example"])
        assert failure.value.stdout == raw[:16384].decode("utf-8", "replace")
        assert failure.value.stderr == "" and failure.value.pid == 42
        assert failure.value.exitcode == 0
        assert client.ping()


def test_successful_run_encodes_input_and_preserves_exit_status():
    text = "日本語🙂\n".encode()

    def handle(request, *_):
        if request["execute"] == "guest-exec":
            assert base64.b64decode(request["arguments"]["input-data"]) == text
            return reply(request, {"pid": 42})
        return reply(
            request, {"exited": True, "signal": 15, "err-data": base64.b64encode(text).decode()}
        )

    with Agent(handle) as peer, QgaClient(peer.path) as client:
        assert client.run(["example"], input_data=text) == (-15, "", text.decode())


def test_waiting_for_another_caller_is_bounded_without_closing_its_connection():
    entered = threading.Event()

    def handle(request, _, stopping):
        if request["execute"] == "slow":
            entered.set()
            stopping.wait(0.2)
        return reply(request, {})

    with Agent(handle) as peer, QgaClient(peer.path, timeout=0.5) as client:
        with ThreadPoolExecutor(max_workers=1) as executor:
            slow = executor.submit(client.execute, "slow")
            assert entered.wait(1)
            with pytest.raises(QgaTimeoutError):
                client.run(["example"], timeout=0.05)
            assert slow.result(timeout=1) == {}
        assert client.ping()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeouts_fail_before_connecting(timeout):
    with pytest.raises(ValueError):
        QgaClient("unused.sock", timeout=timeout)
