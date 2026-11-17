import asyncio
import itertools
import threading
from types import SimpleNamespace

import pytest
from httpx import ASGITransport

from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.state import AgentState
from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from tests.http import AuthenticatedClient


@pytest.fixture
async def snapshot_server(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    made = []
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_vm_operations", server.AsyncKeyedLocks())
    monkeypatch.setattr(server, "_android_provisioner", None)
    monkeypatch.setattr(server, "_resolve_vm", lambda vm: SimpleNamespace(id=vm))
    monkeypatch.setattr(server, "_connect_vm_display", lambda _: FakeDisplayBackend())

    def create(display):
        session = TaskSession(llm=MockLLMProvider(), display=display, store=store, decision=None)
        session.ready, session.finish = asyncio.Event(), asyncio.Event()

        async def wait():
            session.ready.set()
            await session.finish.wait()
            session.loop.context.state = AgentState.COMPLETED

        session._execute_phase = wait
        made.append(session)
        return session

    monkeypatch.setattr(server, "_create_session", create)
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as api:

        async def start(vm):
            response = await api.post("/tasks", json={"instruction": "保存する", "vm_id": vm})
            assert response.status_code == 200
            session = made[-1]
            await asyncio.wait_for(session.ready.wait(), 2)
            return session

        try:
            yield store, api, start
        finally:
            for session in made:
                session.stop()
            await asyncio.gather(*(s._task for s in made if s._task), return_exceptions=True)


async def blocked_scan(store, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = store.latest_per_vm

    def scan():
        records = original()
        entered.set()
        assert release.wait(5)
        return records

    monkeypatch.setattr(store, "latest_per_vm", scan)
    task = asyncio.create_task(server._task_snapshot())
    assert await asyncio.to_thread(entered.wait, 2)
    return task, release


async def test_snapshot_keeps_completion_on_another_vm_during_its_disk_scan(
    snapshot_server, monkeypatch
):
    store, _, start = snapshot_server
    first = await start("A")
    await start("B")
    snapshot, release = await blocked_scan(store, monkeypatch)
    try:
        first.finish.set()
        await first._task
        assert first.id not in server._sessions
    finally:
        release.set()
    states = {task.vm_id: task for task in await snapshot}
    assert states["A"].state == "completed" and not states["A"].is_running
    assert states["B"].is_running


@pytest.mark.parametrize("replace_existing", [False, True])
async def test_snapshot_keeps_a_task_that_starts_and_finishes_during_the_scan(
    snapshot_server, monkeypatch, replace_existing
):
    store, _, start = snapshot_server
    if replace_existing:
        await start("A")
    await start("B")
    snapshot, release = await blocked_scan(store, monkeypatch)
    try:
        newest = await start("A")
        newest.finish.set()
        await newest._task
        await start("C")
    finally:
        release.set()
    states = {task.vm_id: task for task in await snapshot}
    assert states["A"].session_id == newest.id
    assert states["A"].state == "completed" and not states["A"].is_running
    assert states["B"].is_running and states["C"].is_running


@pytest.mark.parametrize("cancel_delete", [False, True])
async def test_snapshot_cannot_restore_history_deleted_during_the_scan(
    snapshot_server, monkeypatch, cancel_delete
):
    store, api, start = snapshot_server
    removed = await start("A")
    removed.finish.set()
    await removed._task
    await start("B")
    snapshot, release = await blocked_scan(store, monkeypatch)
    deleted, finish_delete = threading.Event(), threading.Event()
    original = store.delete

    def delete(task_id):
        result = original(task_id)
        deleted.set()
        assert finish_delete.wait(5)
        return result

    if cancel_delete:
        monkeypatch.setattr(store, "delete", delete)
    request = asyncio.create_task(api.delete("/tasks/" + removed.id))
    try:
        if cancel_delete:
            assert await asyncio.to_thread(deleted.wait, 2)
            request.cancel()
            await asyncio.sleep(0)
            finish_delete.set()
            await asyncio.gather(request, return_exceptions=True)
        else:
            assert (await request).status_code == 200
        assert store.load(removed.id) is None
    finally:
        finish_delete.set()
        release.set()
        await asyncio.gather(request, return_exceptions=True)
    assert {task.vm_id for task in await snapshot} == {"B"}


@pytest.mark.parametrize("cancel_delete", [False, True])
async def test_snapshot_restores_previous_history_when_latest_is_deleted_during_scan(
    snapshot_server, monkeypatch, cancel_delete
):
    store, api, start = snapshot_server
    previous = await start("A")
    previous.finish.set()
    await previous._task
    removed = await start("A")
    removed.finish.set()
    await removed._task
    await start("B")
    snapshot, release = await blocked_scan(store, monkeypatch)
    deleted, finish_delete = threading.Event(), threading.Event()
    original = store.delete

    def delete(task_id):
        result = original(task_id)
        deleted.set()
        assert finish_delete.wait(5)
        return result

    if cancel_delete:
        monkeypatch.setattr(store, "delete", delete)
    request = asyncio.create_task(api.delete("/tasks/" + removed.id))
    try:
        if cancel_delete:
            assert await asyncio.to_thread(deleted.wait, 2)
            request.cancel()
            await asyncio.sleep(0)
            finish_delete.set()
            await asyncio.gather(request, return_exceptions=True)
        else:
            assert (await request).status_code == 200
    finally:
        finish_delete.set()
        release.set()
        await asyncio.gather(request, return_exceptions=True)
    states = {task.vm_id: task for task in await snapshot}
    assert states["A"].session_id == previous.id
    assert states["A"].state == "completed" and not states["A"].is_running
    assert states["B"].is_running


async def test_snapshot_handles_another_deletion_and_new_completion_during_reconciliation(
    snapshot_server, monkeypatch
):
    store, api, start = snapshot_server
    finished = []
    for _ in range(3):
        session = await start("A")
        session.finish.set()
        await session._task
        finished.append(session)
    await start("B")
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    calls = itertools.count()
    original = store.latest_per_vm

    def scan():
        call = next(calls)
        records = original()
        if call < len(entered):
            entered[call].set()
            assert release[call].wait(5)
        return records

    monkeypatch.setattr(store, "latest_per_vm", scan)
    snapshot = asyncio.create_task(server._task_snapshot())
    try:
        assert await asyncio.to_thread(entered[0].wait, 2)
        assert (await api.delete("/tasks/" + finished[2].id)).status_code == 200
        release[0].set()
        assert await asyncio.to_thread(entered[1].wait, 2)
        assert (await api.delete("/tasks/" + finished[1].id)).status_code == 200
        newest = await start("C")
        newest.finish.set()
        await newest._task
        await start("D")
    finally:
        for event in release:
            event.set()
        await asyncio.gather(snapshot, return_exceptions=True)
    states = {task.vm_id: task for task in snapshot.result()}
    assert states["A"].session_id == finished[0].id
    assert states["C"].session_id == newest.id and states["C"].state == "completed"
    assert states["B"].is_running and states["D"].is_running
    assert not server._snapshot_readers


@pytest.mark.parametrize("outcome", ["cancel", "error"])
async def test_snapshot_reconciliation_releases_tracking_on_cancellation_or_error(
    snapshot_server, monkeypatch, outcome
):
    store, api, start = snapshot_server
    removed = await start("A")
    removed.finish.set()
    await removed._task
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    calls = itertools.count()
    original = store.latest_per_vm

    def scan():
        call = next(calls)
        records = original()
        entered[call].set()
        assert release[call].wait(5)
        if call == 1 and outcome == "error":
            raise OSError("reconciliation failed")
        return records

    monkeypatch.setattr(store, "latest_per_vm", scan)
    snapshot = asyncio.create_task(server._task_snapshot())
    try:
        assert await asyncio.to_thread(entered[0].wait, 2)
        assert (await api.delete("/tasks/" + removed.id)).status_code == 200
        release[0].set()
        assert await asyncio.to_thread(entered[1].wait, 2)
        assert (await asyncio.wait_for(api.get("/health"), 1)).status_code == 200
        if outcome == "cancel":
            snapshot.cancel()
            with pytest.raises(asyncio.CancelledError):
                await snapshot
        else:
            release[1].set()
            with pytest.raises(OSError, match="reconciliation failed"):
                await snapshot
        assert not server._snapshot_readers
    finally:
        for event in release:
            event.set()
        await asyncio.gather(snapshot, return_exceptions=True)


async def test_deleting_an_unselected_record_does_not_require_another_full_scan(
    snapshot_server, monkeypatch
):
    store, api, start = snapshot_server
    previous = await start("A")
    previous.finish.set()
    await previous._task
    newest = await start("A")
    newest.finish.set()
    await newest._task
    await start("B")
    calls = 0
    original = store.latest_per_vm

    def scan():
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(store, "latest_per_vm", scan)
    snapshot, release = await blocked_scan(store, monkeypatch)
    try:
        assert (await api.delete("/tasks/" + previous.id)).status_code == 200
    finally:
        release.set()
    states = {task.vm_id: task for task in await snapshot}
    assert states["A"].session_id == newest.id
    assert calls == 1


async def test_overlapping_snapshots_keep_independent_change_tracking(snapshot_server, monkeypatch):
    store, _, start = snapshot_server
    await start("B")
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    calls = itertools.count()
    original = store.latest_per_vm

    def scan():
        call = next(calls)
        records = original()
        entered[call].set()
        assert release[call].wait(5)
        return records

    monkeypatch.setattr(store, "latest_per_vm", scan)
    first = asyncio.create_task(server._task_snapshot())
    second = None
    try:
        assert await asyncio.to_thread(entered[0].wait, 2)
        second = asyncio.create_task(server._task_snapshot())
        assert await asyncio.to_thread(entered[1].wait, 2)
        release[0].set()
        await first
        assert len(server._snapshot_readers) == 1
        finished = await start("A")
        finished.finish.set()
        await finished._task
        await start("C")
    finally:
        for event in release:
            event.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
    states = {task.vm_id: task for task in second.result()}
    assert states["A"].session_id == finished.id and states["A"].state == "completed"
    assert not server._snapshot_readers


async def test_cancelling_a_snapshot_releases_its_tracking_while_disk_io_drains(
    snapshot_server, monkeypatch
):
    store, _, start = snapshot_server
    await start("A")
    snapshot, release = await blocked_scan(store, monkeypatch)
    try:
        snapshot.cancel()
        await asyncio.gather(snapshot, return_exceptions=True)
        assert not server._snapshot_readers
    finally:
        release.set()


async def test_failed_history_scan_releases_its_tracking(snapshot_server, monkeypatch):
    store, _, start = snapshot_server
    await start("A")

    def fail():
        raise OSError("disk read failure")

    monkeypatch.setattr(store, "latest_per_vm", fail)
    with pytest.raises(OSError, match="disk read failure"):
        await server._task_snapshot()
    assert not server._snapshot_readers
