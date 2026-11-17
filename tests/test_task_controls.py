from unittest.mock import Mock

import pytest
from httpx import ASGITransport

from ai_desktop_agent.server import app as server
from tests.http import AuthenticatedClient


@pytest.fixture
def replacement_session(monkeypatch):
    session = Mock(id="replacement", vm_id="desktop", is_running=True, manual_input=None)
    monkeypatch.setattr(server, "_sessions", {session.id: session})
    monkeypatch.setattr(server, "_active_session", session)
    return session


@pytest.mark.parametrize("action", ["pause", "resume", "stop"])
@pytest.mark.parametrize("vm_id", ["desktop", None])
async def test_a_delayed_control_cannot_affect_a_replacement_task(
    replacement_session, action, vm_id
):
    params = {"session_id": "previous"}
    if vm_id is not None:
        params["vm_id"] = vm_id
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        response = await api.post(f"/tasks/current/{action}", params=params)
    assert response.status_code == 409
    getattr(replacement_session, action).assert_not_called()


@pytest.mark.parametrize("action", ["pause", "resume", "stop"])
async def test_a_control_for_a_disappeared_task_is_a_conflict(monkeypatch, action):
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        response = await api.post(
            f"/tasks/current/{action}", params={"vm_id": "desktop", "session_id": "previous"}
        )
    assert response.status_code == 409


@pytest.mark.parametrize("action", ["pause", "resume", "stop"])
async def test_a_session_id_never_overrides_the_selected_vm(replacement_session, action):
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        response = await api.post(
            f"/tasks/current/{action}",
            params={"vm_id": "other", "session_id": replacement_session.id},
        )
    assert response.status_code == 409
    getattr(replacement_session, action).assert_not_called()


@pytest.mark.parametrize("action", ["pause", "resume", "stop"])
@pytest.mark.parametrize("vm_id", ["desktop", None])
async def test_controls_for_the_observed_task_still_work(replacement_session, action, vm_id):
    params = {"session_id": replacement_session.id}
    if vm_id is not None:
        params["vm_id"] = vm_id
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        response = await api.post(f"/tasks/current/{action}", params=params)
    assert response.status_code == 200
    getattr(replacement_session, action).assert_called_once_with()


@pytest.mark.parametrize("action", ["pause", "resume", "stop"])
@pytest.mark.parametrize("session_id", ["", "bad id", "x" * 129])
async def test_invalid_control_targets_cannot_fall_back_to_the_current_task(
    replacement_session, action, session_id
):
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        response = await api.post(
            f"/tasks/current/{action}", params={"vm_id": "desktop", "session_id": session_id}
        )
    assert response.status_code == 422
    getattr(replacement_session, action).assert_not_called()
