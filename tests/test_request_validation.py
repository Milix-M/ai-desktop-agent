import pytest
from httpx import ASGITransport

from ai_desktop_agent.server.app import app
from tests.http import AuthenticatedClient as AsyncClient


@pytest.mark.parametrize("instruction", ["", " \n\t ", "a" * 20001])
async def test_invalid_instructions_are_rejected_before_creating_a_session(instruction):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/tasks", json={"instruction": instruction})
    assert response.status_code == 422


@pytest.mark.parametrize("limit", [0, -1, 1001])
async def test_history_limits_are_bounded(limit):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/tasks?limit={limit}")
    assert response.status_code == 422
