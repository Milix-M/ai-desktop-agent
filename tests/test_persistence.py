import errno
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision
from ai_desktop_agent.agent.state import AgentState
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server import persistence
from ai_desktop_agent.server.persistence import StorageError, atomic_json
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskRecord, TaskStore
from ai_desktop_agent.server.vm_pool import LABEL_PROJECT, VmPool
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AuthenticatedClient as AsyncClient
from tests.test_vm_pool import _FakeContainer, _FakeDocker


def test_failed_atomic_replace_preserves_last_durable_record(tmp_path, monkeypatch):
    path = tmp_path / "record.json"
    atomic_json(path, {"state": "completed"})
    monkeypatch.setattr(
        persistence.os, "replace", lambda *a: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full"))
    )
    with pytest.raises(StorageError):
        atomic_json(path, {"state": "executing"})
    assert json.loads(path.read_text()) == {"state": "completed"}
    assert list(tmp_path.iterdir()) == [path]


def test_history_files_are_private_and_never_contain_partial_json(tmp_path):
    path = tmp_path / "record.json"
    atomic_json(path, {"instruction": "非公開の文書を保存する"})
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["instruction"] == "非公開の文書を保存する"


def test_vm_updates_from_multiple_pool_instances_do_not_lose_allocations(tmp_path):
    path = tmp_path / "vms.json"

    def save(index):
        VmPool(state_file=path)._save_state(f"desk-{index}", {"vnc_port": 5910 + index})

    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(save, range(40)))
    states = json.loads(path.read_text())
    assert set(states) == {f"desk-{i}" for i in range(40)}


@pytest.mark.parametrize("data", ["[]", "{", '{"vm-a":null}'])
def test_corrupt_vm_metadata_is_reported_without_overwriting_it(tmp_path, data):
    path = tmp_path / "vms.json"
    path.write_text(data)
    pool = VmPool(client=_FakeDocker(), state_file=path)
    with pytest.raises(StorageError):
        pool._save_state("vm-new", {"name": "new"})
    assert path.read_text() == data
    with pytest.raises(StorageError):
        pool.list_vms()


def test_unwritable_storage_does_not_fall_back_to_ephemeral_history(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(persistence.tempfile, "TemporaryFile", fail)
    with pytest.raises(StorageError, match="保存先に書き込めません"):
        TaskStore(tmp_path)


def test_delete_failure_is_reported_as_storage_error(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    store.save(TaskRecord("saved", "報告書を保存する", "completed"))
    path_type = type(store._dir)
    monkeypatch.setattr(
        path_type, "unlink", lambda *a, **k: (_ for _ in ()).throw(PermissionError("read-only"))
    )
    with pytest.raises(StorageError, match="削除できません"):
        store.delete("saved")
    assert store.load("saved") is not None


async def test_storage_failure_during_action_stops_future_actions_and_notifies_ui(tmp_path):
    store = TaskStore(tmp_path)
    original = store.save

    def save(record):
        if record.actions:
            raise OSError(errno.ENOSPC, "disk full")
        original(record)

    store.save = save
    display = FakeDisplayBackend()
    llm = MockLLMProvider(
        decide_result=ActionDecision(
            Action(ActionType.TYPE, {"text": "report"}), "input", 1.0, "input"
        )
    )
    session = TaskSession(llm=llm, display=display, store=store, decision=None)
    states, completed = [], []
    session.on_state_change(lambda state, _: states.append(state))
    session.on_complete(completed.append)
    assert await session.run("報告書を保存する") is False
    assert display.text_inputs == ["report"]
    assert llm.decide_call_count == 1
    assert states[-1] == AgentState.FAILED
    assert completed == [False]
    assert not display.is_connected


async def test_failed_completion_write_cannot_report_a_successful_task(tmp_path):
    store = TaskStore(tmp_path)
    original = store.save

    def save(record):
        if record.state == "completed":
            raise OSError(errno.ENOSPC, "disk full")
        original(record)

    store.save = save
    session = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend(), store=store)

    async def finish():
        session.loop.context.state = AgentState.COMPLETED

    session._execute_phase = finish
    assert await session.run("報告書を保存する") is False
    assert session.loop.state == AgentState.FAILED
    assert store.load(session.id).state == "failed"


async def test_task_submission_fails_before_connecting_to_vm_when_storage_is_unwritable(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(
        store,
        "check_writable",
        lambda: (_ for _ in ()).throw(StorageError("保存先に書き込めません")),
    )
    resolve = AsyncMock()
    monkeypatch.setattr(server, "_resolve_vm", resolve)
    async with AsyncClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        response = await client.post("/tasks", json={"instruction": "文書を保存する"})
    assert response.status_code == 503
    assert "保存先" in response.json()["detail"]
    resolve.assert_not_called()


def test_interrupted_tasks_older_than_one_thousand_records_are_not_left_running(tmp_path):
    store = TaskStore(tmp_path)
    record = TaskRecord("old", "保存する", "executing", updated_at=1)
    (store._dir / "old.json").write_text(json.dumps(record.to_dict()))
    for i in range(1001):
        record = TaskRecord(f"done-{i}", "保存する", "completed", updated_at=i + 2)
        (store._dir / f"done-{i}.json").write_text(json.dumps(record.to_dict()))
    assert store.mark_interrupted() == 1
    assert store.load("old").state == "interrupted"


def test_create_failure_rolls_back_its_containers_and_preserves_other_projects(
    tmp_path, monkeypatch
):
    from ai_desktop_agent.server import vm_pool

    monkeypatch.setenv("DESKTOP_IMAGE_REF", "desktop:test")
    docker = _FakeDocker()
    outsider = _FakeContainer("other", labels={LABEL_PROJECT: "other"})
    docker.containers.items.append(outsider)
    pool = VmPool(
        client=docker, state_file=tmp_path / "vms.json", overlays_dir=str(tmp_path / "disks")
    )
    original = vm_pool.atomic_json
    calls = 0

    def fail_final_save(path, value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StorageError("disk full")
        original(path, value)

    monkeypatch.setattr(vm_pool, "atomic_json", fail_final_save)
    with pytest.raises(StorageError):
        pool.create_vm(kind="container")
    assert all(c.removed for c in docker.containers.items if c is not outsider)
    assert not outsider.removed
    assert json.loads((tmp_path / "vms.json").read_text()) == {}
