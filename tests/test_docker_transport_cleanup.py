"""Use a real Docker SDK HTTP session to observe connection release on the wire."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import docker
import pytest

from ai_desktop_agent.server.vm_pool import VmPool


@pytest.mark.parametrize("borrowed", [False, True])
def test_owned_sdk_connections_close_while_borrowed_connections_remain_usable(
    monkeypatch, borrowed
):
    disconnected = threading.Event()

    class DockerHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802
            assert self.path == "/v1.48/_ping"
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")

        def finish(self):
            try:
                super().finish()
            finally:
                disconnected.set()

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), DockerHandler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    client = docker.DockerClient(
        base_url=f"http://127.0.0.1:{http.server_port}", version="1.48", timeout=2
    )
    client.api.trust_env = False
    monkeypatch.setattr(docker, "from_env", Mock(return_value=client))
    try:
        with VmPool(client=client if borrowed else None) as owner:
            assert owner._docker().ping() is True
            assert not disconnected.is_set()
        if borrowed:
            assert not disconnected.wait(0.05)
            assert client.ping() is True
            client.close()
        assert disconnected.wait(2), "the Docker SDK kept the owned HTTP connection open"
    finally:
        client.close()
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)
