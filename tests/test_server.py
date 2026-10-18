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


@pytest.mark.asyncio
class TestWasteReduction:
    def _session(self, llm) -> TaskSession:
        return TaskSession(llm=llm, display=FakeDisplayBackend())

    async def test_empty_storm_short_circuits_second_turn(self):
        from ai_desktop_agent.agent.state import Subtask

        class _Empty(MockLLMProvider):
            def __init__(self):
                super().__init__()
                self.decide_calls = 0

            async def decide_next_action(self, *a, **k):
                self.decide_calls += 1
                raise ValueError("応答が空です")

        llm = _Empty()
        s = self._session(llm)
        sub = Subtask(id="s1", description="d")
        shot = s.display.capture_screen()
        with pytest.raises(ValueError):
            await s._decide_action(sub, shot)
        assert llm.decide_calls == 6  # 初回は従来通り上限まで吸収
        with pytest.raises(ValueError):
            await s._decide_action(sub, shot)
        assert llm.decide_calls == 6 + 2  # 連続失敗時は早期打切り
        assert s._consec_empty_decides == 2

    async def test_success_resets_empty_counter(self):
        from ai_desktop_agent.actions.primitives import Action, ActionType
        from ai_desktop_agent.agent.llm.types import ActionDecision
        from ai_desktop_agent.agent.state import Subtask

        class _Flaky(MockLLMProvider):
            def __init__(self):
                super().__init__()
                self.n = 0

            async def decide_next_action(self, *a, **k):
                self.n += 1
                if self.n == 1:
                    raise ValueError("応答が空です")
                return ActionDecision(
                    action=Action(action_type=ActionType.WAIT, params={"seconds": 0.1}),
                    expected_effect="e",
                    confidence=0.9,
                    reasoning="r",
                )

        llm = _Flaky()
        s = self._session(llm)
        s._consec_empty_decides = 1
        sub = Subtask(id="s1", description="d")
        # 前回失敗扱いのため最大2試行。1回失敗→2回目成功で復帰する
        out = await s._decide_action(sub, s.display.capture_screen())
        assert out.action.action_type == ActionType.WAIT
        assert s._consec_empty_decides == 0

    async def test_wait_action_skips_post_sleep(self, monkeypatch):
        import asyncio as _asyncio

        from ai_desktop_agent.actions.primitives import Action, ActionType
        from ai_desktop_agent.agent.llm.types import ActionDecision
        from ai_desktop_agent.agent.state import Goal, Subtask

        calls: list[float] = []
        real_sleep = _asyncio.sleep

        async def _rec(d):
            calls.append(d)
            await real_sleep(0)

        monkeypatch.setattr(_asyncio, "sleep", _rec)
        llm = MockLLMProvider(
            decide_result=ActionDecision(
                action=Action(action_type=ActionType.WAIT, params={"seconds": 0.1}),
                expected_effect="e",
                confidence=0.9,
                reasoning="r",
            )
        )
        s = self._session(llm)
        s.loop.start(Goal(description="g"))
        s.loop.understanding_done()
        s.loop.plan_ready([Subtask(id="s1", description="d")])
        await s._execute_phase()
        assert 0.1 in calls  # executorの待機自体は残る
        assert 0.5 not in calls  # 追加の据え置き待機はしない

    async def test_still_wait_quick_on_stable(self):
        import time as _time

        from ai_desktop_agent.actions.executor import ActionExecutor

        ex = ActionExecutor(FakeDisplayBackend())
        t0 = _time.monotonic()
        await ex.wait_for_still(5.0)
        assert _time.monotonic() - t0 < 3.0
