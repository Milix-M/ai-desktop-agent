"""Malformed network metadata must fail deletion without discarding saved ownership."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import docker
from httpx import ASGITransport

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.server.vm_pool import VmPool
from tests.http import AuthenticatedClient


async def test_invalid_network_json_preserves_saved_environment_and_allows_deletion_after_repair(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "metadata-delete")
    state_file = tmp_path / "vms.json"
    saved = '{"vm":{"name":"saved environment"}}'
    state_file.write_text(saved)
    responses = {"malformed": True, "network_reads": 0, "mutations": []}

    class Engine(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status, body):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path.removeprefix("/v1.48")
            if path == "/containers/json":
                self.respond(200, b"[]")
            elif path == "/networks/metadata-delete_env_vm":
                responses["network_reads"] += 1
                self.respond(
                    200 if responses["malformed"] else 404,
                    b"{" if responses["malformed"] else b'{"message":"network absent"}',
                )
            else:
                self.respond(404, b'{"message":"not found"}')

        def do_POST(self):
            responses["mutations"].append(("POST", self.path))
            self.respond(500, b'{"message":"unexpected mutation"}')

        def do_DELETE(self):
            responses["mutations"].append(("DELETE", self.path))
            self.respond(500, b'{"message":"unexpected mutation"}')

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Engine)
    thread = threading.Thread(
        target=upstream.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    client = docker.DockerClient(
        base_url=f"http://127.0.0.1:{upstream.server_port}", version="1.48", timeout=1
    )
    pool = VmPool(client=client, state_file=state_file)
    monkeypatch.setattr(server, "_pool", pool)
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_android_provisioner", None)
    try:
        async with AuthenticatedClient(
            transport=ASGITransport(app=server.app), base_url="http://test"
        ) as api:
            failed = await api.delete("/vms/vm")
            assert failed.status_code == 503, failed.text
            assert "Docker" in failed.json()["detail"]
            assert state_file.read_text() == saved
            assert responses["network_reads"] == 1 and responses["mutations"] == []
            responses["malformed"] = False
            repaired = await api.delete("/vms/vm")
            assert repaired.status_code == 200 and repaired.json()["status"] == "deleted"
            assert json.loads(state_file.read_text()) == {}
            assert responses["network_reads"] == 2 and responses["mutations"] == []
    finally:
        pool.close()
        client.close()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)
