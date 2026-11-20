"""Guest-agent failures use real streams and never authorize automatic replay."""

import socket

import pytest

from ai_desktop_agent.vm.qga import MAX_MESSAGE_BYTES, QgaClient, QgaError
from tests.test_qga import Agent, encoded, reply


@pytest.mark.parametrize("kind", ["missing-result", "two-results", "invalid-error"])
def test_invalid_reply_envelope_closes_before_reuse_and_reconnects(kind):
    def handle(request, *_):
        value = {"id": request["id"]}
        if kind == "two-results":
            value.update({"return": {}, "error": {"desc": "ambiguous"}})
        elif kind == "invalid-error":
            value["error"] = ["invalid"]
        return value

    with Agent(handle) as peer, QgaClient(peer.path) as client:
        with pytest.raises(QgaError, match="応答"):
            client.ping()
        assert peer.disconnected.wait(1)
        before = list(peer.requests)
        with pytest.raises(QgaError, match="未接続"):
            client.ping()
        assert peer.requests == before
        peer.handle = lambda request, *_: reply(request, {})
        client.connect()
        assert client.ping()
        assert [r["execute"] for r in peer.requests] == [
            "guest-sync-delimited",
            "guest-ping",
            "guest-sync-delimited",
            "guest-ping",
        ]


@pytest.mark.parametrize("stale", [b"{broken}\n", b"[]\n"], ids=["broken-json", "scalar-root"])
def test_sync_discards_invalid_prefixed_data_before_the_matching_nonce(stale):
    def sync(request, *_):
        return b"\xff" + stale + b"\xff" + encoded(reply(request, request["arguments"]["id"]))

    with Agent(lambda request, *_: reply(request, {"name": "current"}), sync=sync) as peer:
        with QgaClient(peer.path) as client:
            assert client.get_osinfo() == {"name": "current"}
        assert [r["execute"] for r in peer.requests] == ["guest-sync-delimited", "guest-get-osinfo"]


def test_an_event_with_the_current_id_cannot_replace_the_requested_result():
    def handle(request, connection, _):
        connection.sendall(encoded({"event": "STALE", "id": request["id"], "return": "old"}))
        return reply(request, {"name": "current"})

    with Agent(handle) as peer, QgaClient(peer.path) as client:
        assert client.get_osinfo() == {"name": "current"}
        assert client.ping()


@pytest.mark.parametrize("pid", [0, -1, True, "42"])
def test_invalid_status_pid_cannot_send_a_command_or_poison_the_connection(pid):
    with Agent(lambda request, *_: reply(request, {})) as peer, QgaClient(peer.path) as client:
        with pytest.raises(ValueError, match="PID"):
            client.exec_status(pid)
        assert client.ping()
        assert [r["execute"] for r in peer.requests] == ["guest-sync-delimited", "guest-ping"]


@pytest.mark.parametrize(
    "command", [[], [""], ["/bin/echo", 3]], ids=["empty", "no-path", "non-text"]
)
def test_invalid_run_command_cannot_send_or_break_a_following_ping(command):
    with Agent(lambda request, *_: reply(request, {})) as peer, QgaClient(peer.path) as client:
        with pytest.raises(ValueError, match="executable"):
            client.run(command)
        assert client.ping()
        assert [r["execute"] for r in peer.requests] == ["guest-sync-delimited", "guest-ping"]


@pytest.mark.parametrize("pid", [None, 0, -1, True, "42"])
def test_invalid_started_pid_closes_without_querying_or_replaying_the_command(pid):
    with (
        Agent(lambda request, *_: reply(request, {"pid": pid})) as peer,
        QgaClient(peer.path) as client,
    ):
        with pytest.raises(QgaError, match="PID") as failure:
            client.run(["/bin/echo", "report"])
        assert failure.value.pid is None
        assert peer.disconnected.wait(1)
        assert [r["execute"] for r in peer.requests] == ["guest-sync-delimited", "guest-exec"]
        peer.handle = lambda request, *_: reply(request, {})
        client.connect()
        assert client.ping()
        assert sum(r["execute"] == "guest-exec" for r in peer.requests) == 1


def test_shell_command_is_one_shell_argument_and_retains_its_exit_status():
    command = "printf '%s' '四半期報告'; exit 7"

    def handle(request, *_):
        if request["execute"] == "guest-exec":
            assert request["arguments"] == {
                "path": "/bin/sh",
                "arg": ["-c", command],
                "capture-output": True,
            }
            return reply(request, {"pid": 42})
        return reply(request, {"exited": True, "exitcode": 7})

    with Agent(handle) as peer, QgaClient(peer.path) as client:
        assert client.run(command) == (7, "", "")
        assert [r["execute"] for r in peer.requests] == [
            "guest-sync-delimited",
            "guest-exec",
            "guest-exec-status",
        ]


@pytest.mark.parametrize("stage", ["guest-exec", "guest-exec-status"])
def test_eof_retains_a_known_pid_and_requires_sync_before_a_following_command(stage):
    def handle(request, connection, _):
        if request["execute"] == stage:
            connection.shutdown(socket.SHUT_RDWR)
            return None
        return reply(request, {"pid": 42})

    with Agent(handle) as peer, QgaClient(peer.path) as client:
        with pytest.raises(QgaError, match="切断") as failure:
            client.run(["/bin/echo", "report"])
        assert failure.value.pid == (None if stage == "guest-exec" else 42)
        assert peer.disconnected.wait(1)
        before = list(peer.requests)
        with pytest.raises(QgaError, match="未接続"):
            client.ping()
        assert peer.requests == before
        peer.handle = lambda request, *_: reply(request, {})
        client.connect()
        assert client.ping()
        assert sum(r["execute"] == "guest-exec" for r in peer.requests) == 1
        assert [r["execute"] for r in peer.requests][-2:] == ["guest-sync-delimited", "guest-ping"]


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_run_timeout_does_not_start_a_process_or_close_the_connection(timeout):
    with Agent(lambda request, *_: reply(request, {})) as peer, QgaClient(peer.path) as client:
        with pytest.raises(ValueError, match="timeout"):
            client.run(["/bin/echo", "report"], timeout=timeout)
        assert client.ping()
        assert [r["execute"] for r in peer.requests] == ["guest-sync-delimited", "guest-ping"]


@pytest.mark.parametrize("excess", [0, 1], ids=["exact-limit", "one-byte-too-long"])
def test_real_production_message_limit_accepts_its_boundary_and_rejects_excess(excess):
    def handle(request, *_):
        body = encoded(reply(request, {"name": "current"})).rstrip(b"\n")
        return body + b" " * (MAX_MESSAGE_BYTES + excess - len(body)) + b"\n"

    with Agent(handle) as peer, QgaClient(peer.path, timeout=3) as client:
        if excess:
            with pytest.raises(QgaError, match="サイズ上限"):
                client.get_osinfo()
            assert peer.disconnected.wait(1)
        else:
            assert client.get_osinfo() == {"name": "current"}
            assert client.ping()
