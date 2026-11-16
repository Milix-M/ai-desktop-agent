from pathlib import Path
from unittest.mock import AsyncMock

import yaml
from httpx import ASGITransport, AsyncClient

from ai_desktop_agent.server.app import app
from ai_desktop_agent.server.browser_access import BrowserOriginMiddleware
from ai_desktop_agent.server.vm_pool import VmPool
from tests.test_vm_pool import _FakeDocker


async def test_untrusted_browser_cannot_read_or_mutate():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for method, path in [("GET", "/health"), ("POST", "/tasks/current/stop")]:
            response = await client.request(
                method, path, headers={"Origin": "https://evil.example"}
            )
            assert response.status_code == 403
        for origin in ["http://localhost:3000", "http://127.0.0.1:3000"]:
            assert (await client.get("/health", headers={"Origin": origin})).status_code == 200
        assert (await client.get("/health")).status_code == 200


async def test_websocket_origin_is_checked_before_accept():
    target, send = AsyncMock(), AsyncMock()
    await BrowserOriginMiddleware(target)(
        {"type": "websocket", "headers": [(b"origin", b"https://evil.example")]},
        AsyncMock(),
        send,
    )
    target.assert_not_awaited()
    send.assert_awaited_once_with({"type": "websocket.close", "code": 1008})


def test_all_compose_ports_are_loopback_only():
    config = yaml.safe_load((Path(__file__).parents[1] / "docker-compose.yml").read_text())
    for service in config["services"].values():
        for port in service.get("ports", []):
            assert port.startswith("127.0.0.1:"), port


def test_dynamic_desktop_and_relay_publish_only_on_loopback(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKTOP_IMAGE_REF", "desktop:test")
    client = _FakeDocker()
    pool = VmPool(client=client, state_file=tmp_path / "state.json")
    pool.create_vm(kind="container")
    ports = [binding for run in client.containers.runs for binding in run.get("ports", {}).values()]
    assert ports
    assert all(binding[0] == "127.0.0.1" for binding in ports)
