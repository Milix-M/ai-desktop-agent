import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import MAX_SESSION_ORDER, TaskRecord, TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend


def test_order_is_unique_across_store_instances_and_survives_restart(tmp_path):
    stores = [TaskStore(tmp_path) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=8) as workers:
        orders = list(workers.map(lambda i: stores[i % 4].reserve_session_order(), range(30)))
    assert sorted(orders) == list(range(1, 31))
    assert TaskStore(tmp_path).reserve_session_order() == 31
    assert stores[0]._order_file.stat().st_mode & 0o777 == 0o600


def test_snapshot_fence_does_not_consume_an_order_and_survives_history_deletion(tmp_path):
    store = TaskStore(tmp_path)
    assert store.current_session_order() == 0
    assert store.reserve_session_order() == 1
    store.save(TaskRecord("task", "文書を保存する", "completed", session_order=1))
    assert store.delete("task")
    restored = TaskStore(tmp_path)
    assert restored.current_session_order() == 1
    assert restored.current_session_order() == 1
    assert restored.reserve_session_order() == 2


@pytest.mark.parametrize("counter_exists", [False, True])
def test_restored_history_repairs_a_missing_or_older_order_counter(tmp_path, counter_exists):
    store = TaskStore(tmp_path)
    store.save(TaskRecord("restored", "文書を保存する", "completed", session_order=42))
    if counter_exists:
        store._order_file.write_text('{"last_order":4}')
    assert TaskStore(tmp_path).reserve_session_order() == 43
    assert store.reserve_session_order() == 44


@pytest.mark.parametrize(
    "data",
    ['{"last_order":true}', '{"last_order":-1}', "[]", "{", '{"last_order":9007199254740991}'],
)
def test_corrupt_order_counter_stops_new_tasks_without_overwriting_it(tmp_path, data):
    store = TaskStore(tmp_path)
    store._order_file.write_text(data)
    with pytest.raises(StorageError):
        store.reserve_session_order()
    assert store._order_file.read_text() == data


def test_clock_rollback_does_not_make_an_older_session_the_latest(tmp_path):
    store = TaskStore(tmp_path)
    for task_id, order, updated_at in [("old", 1, 100), ("new", 2, 10), ("legacy", 0, 1000)]:
        record = TaskRecord(
            task_id,
            "文書を保存する",
            "completed",
            vm_id="desktop",
            session_order=order,
            updated_at=updated_at,
        )
        (store._dir / f"{task_id}.json").write_text(json.dumps(record.to_dict()))
    assert store.latest().id == "new"
    assert [r.id for r in store.list()] == ["new", "old", "legacy"]
    assert [r["id"] for r in store.summaries()] == ["new", "old", "legacy"]
    assert store.latest_per_vm()[0]["id"] == "new"


@pytest.mark.parametrize("order", [-1, True, MAX_SESSION_ORDER + 1])
def test_invalid_order_is_skipped(tmp_path, order):
    store = TaskStore(tmp_path)
    record = TaskRecord("broken", "文書を保存する", "completed").to_dict()
    record["session_order"] = order
    (store._dir / "broken.json").write_text(json.dumps(record))
    assert store.load("broken") is None


async def test_session_publishes_one_durable_order_before_calling_the_model(tmp_path):
    store = TaskStore(tmp_path)
    events = []

    class WaitingLLM(MockLLMProvider):
        async def understand_instruction(self, *args, **kwargs):
            assert events and events[0] > 0
            record = store.load(session.id)
            assert record.session_order == session.session_order == events[0]
            await asyncio.Event().wait()

    session = TaskSession(llm=WaitingLLM(), display=FakeDisplayBackend(), store=store)
    session.on_state_change(lambda *_: events.append(session.session_order))
    await session.start_async("文書を保存する")
    session.stop()
    await asyncio.gather(session._task, return_exceptions=True)
    assert events and all(order == events[0] for order in events)
    assert store.load(session.id).session_order == events[0]
    assert TaskStore(tmp_path).reserve_session_order() > events[0]
