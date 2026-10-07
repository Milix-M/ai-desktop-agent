"""タスク永続化（store + 履歴API）のテスト。"""

import pytest
from httpx import ASGITransport, AsyncClient

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.state import Goal, Subtask
from ai_desktop_agent.server import app as server_app
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import ACTIVE_STATES, TaskRecord, TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend


@pytest.fixture
def store(tmp_path):
    return TaskStore(root=tmp_path / "data")


def _record(task_id="abc123") -> TaskRecord:
    from ai_desktop_agent.server.store import StoredAction

    return TaskRecord(
        id=task_id,
        instruction="テスト指示",
        state="completed",
        success=True,
        actions=[
            StoredAction(
                action_type="left_click",
                params={"x": 1, "y": 2},
                description="左クリック",
                success=True,
            )
        ],
    )


class TestTaskStore:
    def test_save_and_load(self, store):
        store.save(_record())
        rec = store.load("abc123")
        assert rec is not None
        assert rec.instruction == "テスト指示"
        assert rec.actions[0].params == {"x": 1, "y": 2}

    def test_load_missing_returns_none(self, store):
        assert store.load("nope") is None

    def test_list_newest_first(self, store):
        store.save(_record("old"))
        store.save(_record("new"))
        ids = [r.id for r in store.list()]
        assert ids == ["new", "old"]

    def test_mark_interrupted(self, store):
        rec = _record("run1")
        rec.state = "executing"
        store.save(rec)
        store.save(_record("done1"))
        assert store.mark_interrupted() == 1
        assert store.load("run1").state == "interrupted"
        assert store.load("run1").success is False
        assert store.load("done1").state == "completed"

    def test_active_states_covered(self):
        assert {"executing", "verifying", "recovering", "paused"} <= ACTIVE_STATES


class TestSessionPersistence:
    @pytest.mark.asyncio
    async def test_run_persists_record(self, tmp_path):
        store = TaskStore(root=tmp_path / "data")
        session = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend(), store=store)
        await session.run("永続化テスト")
        rec = store.load(session.id)
        assert rec is not None
        assert rec.instruction == "永続化テスト"
        assert rec.state == "completed"

    def test_snapshot_without_store(self):
        session = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend())
        session.loop.start(Goal(description="x"))
        rec = session.snapshot()
        assert rec.instruction == ""
        assert rec.state == "understanding"


@pytest.fixture
def _hist_app(tmp_path):
    from ai_desktop_agent.server.vm_pool import VmInfo

    server_app._active_session = None
    server_app._sessions = {}
    server_app._store = TaskStore(root=tmp_path / "data")
    orig = server_app._create_session

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
    orig_connect = server_app._connect_vm_display
    server_app._connect_vm_display = lambda vm: FakeDisplayBackend()
    server_app._create_session = lambda: TaskSession(
        llm=MockLLMProvider(), display=FakeDisplayBackend(), store=server_app._store
    )
    yield
    server_app._active_session = None
    server_app._sessions = {}
    server_app._create_session = orig
    server_app._store = None
    server_app._pool = None
    server_app._connect_vm_display = orig_connect


@pytest.mark.asyncio
class TestHistoryEndpoints:
    async def test_empty_history(self, _hist_app):
        transport = ASGITransport(app=server_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/tasks")
            assert resp.status_code == 200
            assert resp.json() == []

    async def test_history_after_task(self, _hist_app):
        transport = ASGITransport(app=server_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/tasks", json={"instruction": "履歴テスト"})
            # バックグラウンドタスクの完了を待ってから履歴を確認
            session = server_app._active_session
            assert session is not None
            await session._task
            resp = await client.get("/tasks")
            assert resp.status_code == 200
            items = resp.json()
            assert len(items) == 1
            assert items[0]["instruction"] == "履歴テスト"

            detail = await client.get(f"/tasks/{items[0]['id']}")
            assert detail.status_code == 200
            assert detail.json()["instruction"] == "履歴テスト"

    async def test_current_falls_back_to_latest(self, _hist_app):
        transport = ASGITransport(app=server_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/tasks", json={"instruction": "フォールバック"})
            session = server_app._active_session
            assert session is not None
            await session._task
            server_app._active_session = None  # 再起動を模擬
            resp = await client.get("/tasks/current")
            assert resp.status_code == 200
            data = resp.json()
            assert data["is_running"] is False
            assert data["state"] == "completed"

    async def test_detail_404(self, _hist_app):
        transport = ASGITransport(app=server_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/tasks/does-not-exist")
            assert resp.status_code == 404

    def test_snapshot_action_serialization(self):
        session = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend())
        session.loop.start(Goal(description="g"))
        session.loop.understanding_done()
        session.loop.plan_ready([Subtask(id="s1", description="d")])
        session.loop.record_action(
            Action(action_type=ActionType.LEFT_CLICK, params={"x": 1, "y": 2}), True
        )
        rec = session.snapshot()
        assert rec.actions[0].action_type == "left_click"
        assert rec.subtasks[0]["id"] == "s1"


class TestDeleteTask:
    def test_delete_removes_file(self, tmp_path):
        from ai_desktop_agent.server.store import TaskStore

        store = TaskStore(root=tmp_path / "data")
        store.save(_record("del1"))
        assert store.delete("del1") is True
        assert store.load("del1") is None
        assert store.delete("del1") is False

    @pytest.mark.asyncio
    async def test_delete_endpoint(self, _hist_app):
        transport = ASGITransport(app=server_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/tasks", json={"instruction": "消す"})
            session = server_app._active_session
            assert session is not None
            await session._task
            task_id = session.id
            server_app._active_session = None
            resp = await client.delete(f"/tasks/{task_id}")
            assert resp.status_code == 200
            assert (await client.get("/tasks")).json() == []
            resp2 = await client.delete(f"/tasks/{task_id}")
            assert resp2.status_code == 404


class TestSubtaskProgress:
    def test_snapshot_carries_index(self, tmp_path):
        from ai_desktop_agent.agent.llm.mock import MockLLMProvider
        from ai_desktop_agent.agent.state import Goal, Subtask
        from ai_desktop_agent.server.session import TaskSession
        from ai_desktop_agent.server.store import TaskStore
        from ai_desktop_agent.vm.fake import FakeDisplayBackend

        store = TaskStore(root=tmp_path / "data")
        session = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend(), store=store)
        session.loop.start(Goal(description="g"))
        session.loop.understanding_done()
        session.loop.plan_ready(
            [Subtask(id="s1", description="a"), Subtask(id="s2", description="b")]
        )
        rec = session.snapshot()
        assert rec.current_subtask_index == 0
        assert [s["id"] for s in rec.subtasks] == ["s1", "s2"]
        store.save(rec)
        d = store.load(rec.id).to_dict()
        assert d["current_subtask_index"] == 0
        assert len(d["subtasks"]) == 2

    @pytest.mark.asyncio
    async def test_current_includes_subtasks(self, _hist_app):
        transport = ASGITransport(app=server_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/tasks", json={"instruction": "進捗テスト"})
            session = server_app._active_session
            assert session is not None
            await session._task
            resp = await client.get("/tasks/current")
            data = resp.json()
            assert data["subtasks"][0]["description"] == "進捗テスト"
            assert data["current_subtask_index"] >= 0
