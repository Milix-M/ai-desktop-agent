import asyncio
import json
import threading
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.state import AgentState, Goal, Subtask
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import StoredAction, TaskRecord, TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AuthenticatedClient


async def test_slow_task_save_does_not_block_other_requests(tmp_path):
    store = TaskStore(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = store.save

    def save(record):
        entered.set()
        assert release.wait(2)
        original(record)

    store.save = save
    session = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend(), store=store)
    timer = threading.Timer(0.5, release.set)
    timer.start()
    startup = asyncio.create_task(session.start_async("保存処理を確認する"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        async with AuthenticatedClient(
            transport=ASGITransport(app=server.app), base_url="http://test"
        ) as api:
            assert (await api.get("/health")).status_code == 200
        assert not release.is_set(), "the event loop waited for the disk write"
    finally:
        release.set()
        timer.cancel()
        await startup
        session.stop()
        await asyncio.gather(session._task, return_exceptions=True)


@pytest.mark.parametrize("operation", ["load", "delete"])
async def test_history_access_does_not_block_other_requests(tmp_path, monkeypatch, operation):
    store = TaskStore(tmp_path)
    store.save(TaskRecord("saved", "保存する", "completed"))
    original = getattr(store, operation)
    entered, release = threading.Event(), threading.Event()

    def blocked(task_id):
        entered.set()
        assert release.wait(2)
        return original(task_id)

    monkeypatch.setattr(store, operation, blocked)
    monkeypatch.setattr(server, "get_store", lambda: store)
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_sessions", {})
    timer = threading.Timer(0.5, release.set)
    timer.start()
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        request = asyncio.create_task(
            api.get("/tasks/saved") if operation == "load" else api.delete("/tasks/saved")
        )
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            assert (await api.get("/health")).status_code == 200
            assert not release.is_set(), "history I/O blocked the event loop"
        finally:
            release.set()
            timer.cancel()
            await request


@pytest.mark.parametrize(
    "field,value",
    [
        ("updated_at", float("nan")),
        ("updated_at", float("inf")),
        ("updated_at", 10**400),
        ("vm_id", {}),
        ("success", {}),
        ("id", "../outside"),
        ("id", "other-valid-id"),
        ("actions", [{"action_type": "wait", "params": {}, "description": "", "success": {}}]),
        ("goal", {"nested": float("nan")}),
    ],
)
async def test_invalid_history_cannot_break_the_list_or_detail_endpoint(
    tmp_path, monkeypatch, field, value
):
    store = TaskStore(tmp_path)
    broken = TaskRecord("broken", "壊れた履歴", "executing").to_dict()
    broken[field] = value
    (store._dir / "broken.json").write_text(json.dumps(broken))
    store.save(TaskRecord("valid", "保存する", "completed"))
    monkeypatch.setattr(server, "get_store", lambda: store)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        assert [item["id"] for item in (await api.get("/tasks")).json()] == ["valid"]
        assert (await api.get("/tasks/broken")).status_code == 404


async def test_deleting_a_task_drains_its_pending_save_before_unlink(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = store.save
    writes = []

    def save(record):
        if not writes:
            entered.set()
            assert release.wait(3)
        writes.append(record.state)
        original(record)

    store.save = save
    session = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend(), store=store)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_sessions", {session.id: session})
    monkeypatch.setattr(server, "_active_session", session)
    startup = asyncio.create_task(session.start_async("保存する"))
    deletion = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        deletion = asyncio.create_task(server.delete_task(session.id))
        await asyncio.sleep(0.02)
        assert not deletion.done()
        assert store.load(session.id) is None
    finally:
        release.set()
        await startup
        if deletion is not None:
            assert await deletion == {"status": "deleted"}
        else:
            session.stop()
            await asyncio.gather(session._task, return_exceptions=True)
    assert writes == ["understanding", "failed"]
    assert store.load(session.id) is None
    assert not session.display.is_connected


async def test_initial_save_failure_is_reported_without_calling_the_model(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    llm = MockLLMProvider()
    llm.understand_instruction = AsyncMock()
    display = FakeDisplayBackend()
    session = TaskSession(llm=llm, display=display, store=store)

    def fail(record):
        raise StorageError("disk full")

    store.save = fail
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_connect_vm_display", lambda vm: display)
    monkeypatch.setattr(server, "_create_session", lambda display: session)
    vm = SimpleNamespace(id="desktop")
    with pytest.raises(StorageError):
        await server._start_task_on_vm(server.CreateTaskRequest(instruction="保存する"), vm)
    await asyncio.sleep(0)
    assert not server._sessions
    assert session.loop.state == AgentState.FAILED
    assert not display.is_connected
    llm.understand_instruction.assert_not_called()


async def test_pause_save_failure_returns_503_and_stops_the_owner(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    original = store.save

    def save(record):
        if record.state == "paused":
            raise StorageError("disk full")
        original(record)

    store.save = save
    session = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend(), store=store)
    executing = asyncio.Event()

    async def wait():
        executing.set()
        await asyncio.Event().wait()

    session._execute_phase = wait
    monkeypatch.setattr(server, "_active_session", session)
    await session.start_async("保存する")
    await asyncio.wait_for(executing.wait(), 1)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        assert (await api.post("/tasks/current/pause")).status_code == 503
    await asyncio.wait_for(asyncio.gather(session._task, return_exceptions=True), 1)
    record = store.load(session.id)
    assert record.state == "failed"
    assert record.stop_reason == "storage_error"
    assert not session.display.is_connected


async def test_concurrent_saves_are_ordered_and_freeze_nested_action_params(tmp_path):
    from ai_desktop_agent.actions.primitives import Action, ActionType

    store = TaskStore(tmp_path)
    entered, release = threading.Event(), threading.Event()
    saved = []
    running = 0
    maximum = 0

    def save(record):
        nonlocal running, maximum
        running += 1
        maximum = max(maximum, running)
        if not saved:
            entered.set()
            assert release.wait(3)
        saved.append(record)
        running -= 1

    store.save = save
    session = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend(), store=store)
    session.loop.start(Goal("保存する"))
    session.loop.understanding_done()
    session.loop.plan_ready([Subtask("s", "保存する")])
    params = {"text": "before"}
    action = Action(ActionType.TYPE, params)
    # Simulate a future structured parameter without involving action execution.
    params["extra"] = {"nested": ["before"]}
    session.loop.record_action(action, True)
    first = asyncio.create_task(session._persist())
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        params["extra"]["nested"].append("after")
        session.loop.pause()
        second = asyncio.create_task(session._persist())
        await asyncio.sleep(0)
        assert maximum == 1
    finally:
        release.set()
        await first
        if second:
            await second
        await session.aclose()
    assert maximum == 1
    assert [r.state for r in saved] == ["executing", "paused"]
    assert saved[0].revision < saved[1].revision
    assert saved[0].actions[0].params["extra"]["nested"] == ["before"]
    assert saved[1].actions[0].params["extra"]["nested"] == ["before", "after"]


@pytest.mark.parametrize("operation", ["list", "summaries", "latest_per_vm", "mark_interrupted"])
def test_scanning_history_does_not_retain_all_action_histories(tmp_path, monkeypatch, operation):
    store = TaskStore(tmp_path)
    for index in range(50):
        (store._dir / f"task-{index}.json").write_text("{}")
    live = peak = 0

    def released():
        nonlocal live
        live -= 1

    def load(task_id):
        nonlocal live, peak
        index = int(task_id.split("-")[-1])
        record = TaskRecord(
            task_id,
            "保存する",
            "completed",
            updated_at=index,
            vm_id=f"vm-{index % 2}",
            actions=[StoredAction("type", {"text": "large payload"}, "入力する", index % 2 == 0)],
        )
        live += 1
        peak = max(peak, live)
        weakref.finalize(record, released)
        return record

    monkeypatch.setattr(store, "load", load)
    if operation == "list":
        assert [r.id for r in store.list(limit=3)] == ["task-49", "task-48", "task-47"]
    elif operation == "summaries":
        items = store.summaries(limit=3)
        assert [item["id"] for item in items] == ["task-49", "task-48", "task-47"]
        assert all("actions" not in item and item["action_count"] == 1 for item in items)
    elif operation == "latest_per_vm":
        items = store.latest_per_vm()
        assert [item["id"] for item in items] == ["task-49", "task-48"]
        assert [item["success_count"] for item in items] == [0, 1]
        assert all("actions" not in item for item in items)
    else:
        assert store.mark_interrupted() == 0
    assert peak <= 6, f"{operation} retained {peak} full action histories"


def test_startup_never_rewrites_a_record_with_mismatched_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-history-credential")
    store = TaskStore(tmp_path)
    path = store._dir / "broken.json"
    original = json.dumps(
        TaskRecord("other", "synthetic-history-credential", "executing").to_dict()
    )
    path.write_text(original)
    assert store.redact_existing() == 0
    assert store.mark_interrupted() == 0
    assert path.read_text() == original
    assert list(store._dir.glob("*.json")) == [path]


def test_startup_skips_excessively_nested_history_without_rewriting_it(tmp_path):
    store = TaskStore(tmp_path)
    path = store._dir / "broken.json"
    original = (
        '{"id":"broken","state":"executing","goal":{"nested":' + "[" * 600 + "0" + "]" * 600 + "}}"
    )
    path.write_text(original)
    assert store.redact_existing() == 0
    assert store.mark_interrupted() == 0
    assert path.read_text() == original


async def test_stop_before_run_starts_persists_failure_and_unblocks_submission(tmp_path):
    store = TaskStore(tmp_path)
    session = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend(), store=store)
    startup = asyncio.create_task(session.start_async("保存する"))
    asyncio.get_running_loop().call_soon(session.stop)
    await asyncio.wait_for(startup, 1)
    await session._cleanup_task
    assert session._task.cancelled()
    assert store.load(session.id).state == "failed"
    assert not session.display.is_connected


async def test_repeated_stop_drains_terminal_write_and_still_notifies_ui(tmp_path):
    store = TaskStore(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = store.save

    def save(record):
        if record.state == "failed":
            entered.set()
            assert release.wait(3)
        original(record)

    store.save = save
    llm = MockLLMProvider()

    async def wait(*args, **kwargs):
        await asyncio.Event().wait()

    llm.understand_instruction = wait
    llm.aclose = AsyncMock()
    session = TaskSession(llm=llm, display=FakeDisplayBackend(), store=store)
    completed = []
    session.on_complete(completed.append)
    await session.start_async("保存する")
    session.stop()
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        session.stop()
        await asyncio.sleep(0)
        assert not session._task.done()
    finally:
        release.set()
        await asyncio.gather(session._task, return_exceptions=True)
    assert store.load(session.id).state == "failed"
    assert completed == [False]
    assert not session.display.is_connected
    llm.aclose.assert_awaited_once()
