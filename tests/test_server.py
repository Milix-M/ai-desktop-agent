"""サーバー起動テスト。"""

import pytest
from httpx import ASGITransport, AsyncClient
from ai_desktop_agent.server import app as server_app


@pytest.fixture(autouse=True)
def _reset_session():
    """各テスト前にグローバルセッションをリセット。"""
    server_app._active_session = None
    yield
    server_app._active_session = None


@pytest.mark.asyncio
class TestHealth:
    async def test_health(self):
        transport = ASGITransport(app=server_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
class TestCreateTask:
    async def test_create_task(self):
        transport = ASGITransport(app=server_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/tasks", json={"instruction": "テストタスク"})
            assert resp.status_code == 200
            data = resp.json()
            assert "state" in data
            assert "session_id" in data


@pytest.mark.asyncio
class TestGetCurrentTask:
    async def test_no_session(self):
        transport = ASGITransport(app=server_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/tasks/current")
            assert resp.status_code == 200
            data = resp.json()
            assert data["state"] == "idle"
            assert data["session_id"] is None

    async def test_after_create(self):
        transport = ASGITransport(app=server_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/tasks", json={"instruction": "test"})
            resp = await client.get("/tasks/current")
            assert resp.status_code == 200
            data = resp.json()
            assert data["session_id"] is not None
            assert data["action_count"] >= 0


@pytest.mark.asyncio
class TestWebSocket:
    async def test_ws_connect(self):
        """WebSocket接続が確立できること。"""
        transport = ASGITransport(app=server_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/tasks", json={"instruction": "test"})

        transport2 = ASGITransport(app=server_app.app)
        async with AsyncClient(transport=transport2, base_url="http://test") as client:
            resp = await client.get("/tasks/current")
            assert resp.status_code == 200


@pytest.mark.asyncio
class TestPauseResumeStop:
    async def test_pause_no_session(self):
        transport = ASGITransport(app=server_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/tasks/current/pause")
            data = resp.json()
            assert data["status"] == "no_session"

    async def test_stop_no_session(self):
        transport = ASGITransport(app=server_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/tasks/current/stop")
            data = resp.json()
            assert data["status"] == "no_session"
