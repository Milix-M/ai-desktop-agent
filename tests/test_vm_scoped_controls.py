from unittest.mock import Mock

import pytest
from httpx import ASGITransport

from ai_desktop_agent.server import app as server
from tests.http import AuthenticatedClient as AsyncClient


@pytest.mark.parametrize("action", ["pause", "resume", "stop"])
async def test_control_targets_selected_vm(action, monkeypatch):
    first, last = Mock(vm_id="A", is_running=True), Mock(vm_id="B", is_running=True)
    monkeypatch.setattr(server, "_sessions", {"first": first, "last": last})
    monkeypatch.setattr(server, "_active_session", last)
    async with AsyncClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        response = await client.post(f"/tasks/current/{action}?vm_id=A")
        assert response.status_code == 200
        getattr(first, action).assert_called_once()
        getattr(last, action).assert_not_called()
        response = await client.post(f"/tasks/current/{action}?vm_id=missing")
        assert response.json() == {"status": "no_session"}
        getattr(last, action).assert_not_called()
