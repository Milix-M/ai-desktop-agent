"""サーバー起動テスト。"""

import pytest
from httpx import ASGITransport, AsyncClient

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.server import app as server_app
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.vm.fake import FakeDisplayBackend


@pytest.fixture(autouse=True)
def _reset_session(tmp_path):
    """各テスト前にグローバルセッションをリセットし、モックを使うようにする。"""
    from ai_desktop_agent.server.store import TaskStore
    from ai_desktop_agent.server.vm_pool import VmInfo

    server_app._active_session = None
    server_app._sessions = {}
    server_app._store = TaskStore(root=tmp_path / "data")

    class _FakePool:
        def get_vm(self, vm_id):
            return (
                VmInfo(id="vm", name="vm", status="running", vnc_host="vm", managed=False)
                if vm_id == "vm"
                else None
            )

        def default_vm(self):
            return VmInfo(id="vm", name="vm", status="running", vnc_host="vm", managed=False)

    server_app._pool = _FakePool()
    _orig_connect = server_app._connect_vm_display
    server_app._connect_vm_display = lambda vm: FakeDisplayBackend()
    # セッションファクトリをモックに差し替え（本番コードパスはそのまま）
    server_app._create_session = lambda: TaskSession(
        llm=MockLLMProvider(),
        display=FakeDisplayBackend(),
        store=server_app._store,
    )
    yield
    server_app._active_session = None
    server_app._sessions = {}
    server_app._create_session = server_app._default_create_session  # デフォルトに戻す
    server_app._store = None
    server_app._pool = None
    server_app._connect_vm_display = _orig_connect


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

    async def test_hung_send_does_not_block(self):
        """死んだ接続への送信は打ち切られ、呼び出し側は固まらない。"""
        import asyncio
        import time

        from ai_desktop_agent.server.app import _send_json

        class _HungWS:
            async def send_json(self, payload):
                await asyncio.sleep(3600)

        start = time.monotonic()
        await asyncio.wait_for(_send_json(_HungWS(), {"type": "ping"}), timeout=10.0)
        assert time.monotonic() - start < 10.0

    async def test_send_records_payload(self):
        """正常接続にはペイロードが届く。"""
        from ai_desktop_agent.server.app import _send_json

        class _OkWS:
            def __init__(self):
                self.sent = []

            async def send_json(self, payload):
                self.sent.append(payload)

        ws = _OkWS()
        await _send_json(ws, {"type": "ping"})
        assert ws.sent == [{"type": "ping"}]


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
