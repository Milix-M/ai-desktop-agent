import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server import schedules
from ai_desktop_agent.server.schedules import CreateSchedule, Scheduler
from tests.http import TEST_ACCESS_KEY, AuthenticatedClient


async def test_schedule_api_dispatch_and_restart(tmp_path, monkeypatch):
    now = 100
    monkeypatch.setattr(schedules, "time", SimpleNamespace(time=lambda: now))
    submit = AsyncMock(return_value=SimpleNamespace(session_id="task-1"))
    monkeypatch.setattr(server, "create_task", submit)
    scheduler = Scheduler(server._submit_scheduled_task, tmp_path)
    scheduler.initialize()
    monkeypatch.setattr(server.app.state, "scheduler", scheduler, raising=False)
    async with AuthenticatedClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as api:
        payload = {
            "vm_id": "vm-1",
            "instruction": "件数を確認",
            "run_at": 200,
            "request_id": str(uuid4()),
        }
        response = await api.post("/schedules", json=payload)
        assert response.status_code == 201
        item = response.json()
        assert (await api.post("/schedules", json=payload)).json() == item
        conflict = await api.post("/schedules", json=payload | {"instruction": "別の作業"})
        assert conflict.status_code == 409
        for change in ({"run_at": 99}, {"interval": 2}, {"instruction": TEST_ACCESS_KEY}):
            invalid = await api.post("/schedules", json=payload | {"request_id": None} | change)
            assert invalid.status_code == 422
        await scheduler.dispatch_due()
        submit.assert_not_awaited()
        now = 210
        restarted = Scheduler(server._submit_scheduled_task, tmp_path)
        restarted.initialize()
        await asyncio.gather(restarted.dispatch_due(), restarted.dispatch_due())
        submit.assert_awaited_once_with(
            server.CreateTaskRequest(vm_id="vm-1", instruction="件数を確認", replace_running=False),
            f"schedule-{item['id']}-200",
        )
        [finished] = (await api.get("/schedules")).json()
        assert finished["next_run"] is None
        assert finished["last_task_id"] == "task-1"
        assert (await api.delete("/schedules/" + item["id"])).is_success
        assert (await api.get("/schedules")).json() == []


@pytest.mark.parametrize("failure", [HTTPException(409, "実行中"), RuntimeError()])
async def test_recurring_schedule_coalesces_missed_runs_and_records_failure(
    tmp_path, monkeypatch, failure
):
    now = 100
    monkeypatch.setattr(schedules, "time", SimpleNamespace(time=lambda: now))
    submit = AsyncMock(side_effect=failure)
    scheduler = Scheduler(submit, tmp_path)
    scheduler.initialize()
    item = await scheduler.add(
        CreateSchedule(vm_id="desktop", instruction="確認", run_at=200, interval=86400)
    )
    now = 3 * 86400 + 201
    await scheduler.dispatch_due()
    await scheduler.dispatch_due()
    submit.assert_awaited_once()
    [saved] = await scheduler.list_all()
    assert saved.next_run == 4 * 86400 + 200
    assert saved.last_error
    assert saved.last_task_id is None
    await scheduler.remove(item.id)
    now = saved.next_run
    await scheduler.dispatch_due()
    submit.assert_awaited_once()


async def test_worker_creates_one_history_record_after_interrupted_dispatch(tmp_path, monkeypatch):
    from ai_desktop_agent.agent.llm.mock import MockLLMProvider
    from ai_desktop_agent.server.session import TaskSession
    from ai_desktop_agent.server.store import TaskStore
    from ai_desktop_agent.vm.fake import FakeDisplayBackend

    now = 100
    monkeypatch.setattr(schedules, "time", SimpleNamespace(time=lambda: now))
    store = TaskStore(tmp_path)
    session = TaskSession(
        llm=MockLLMProvider(), display=FakeDisplayBackend(), store=store, decision=None
    )
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    monkeypatch.setattr(server, "_resolve_vm", lambda vm_id: SimpleNamespace(id=vm_id))
    monkeypatch.setattr(server, "_connect_vm_display", lambda _: session.display)
    monkeypatch.setattr(server, "_create_session", lambda _: session)
    scheduler = Scheduler(server._submit_scheduled_task, tmp_path)
    scheduler.initialize()
    item = await scheduler.add(
        CreateSchedule(vm_id="desktop", instruction="画面を確認", run_at=200)
    )
    now = 200
    # Recreate a crash between durable task admission and the schedule update.
    task_id = await server._submit_scheduled_task(item, f"schedule-{item.id}-200")
    await asyncio.wait_for(session._task, 5)
    assert store.load(task_id).state == "completed"
    restarted = Scheduler(server._submit_scheduled_task, tmp_path)
    worker = asyncio.create_task(restarted.run())
    try:
        async with asyncio.timeout(5):
            while (await restarted.list_all())[0].next_run is not None:
                await asyncio.sleep(0.01)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    assert (await restarted.list_all())[0].last_task_id == task_id
    assert len(list((tmp_path / "tasks").glob("*.json"))) == 1


async def test_existing_database_migrates_without_changing_fixed_targets(tmp_path):
    import sqlite3
    from contextlib import closing

    scheduler = Scheduler(AsyncMock(), tmp_path)
    scheduler.path.parent.mkdir()
    with closing(sqlite3.connect(scheduler.path)) as db, db:
        db.execute("""CREATE TABLE schedules(id TEXT PRIMARY KEY, instruction TEXT NOT NULL,
            vm_id TEXT NOT NULL,run_at INTEGER,interval INTEGER,next_run INTEGER,
            last_task_id TEXT,last_error TEXT)""")
        db.execute(
            "INSERT INTO schedules VALUES ('fixed','元の予約','desktop',100,0,NULL,'task-1',NULL)"
        )
    scheduler.initialize()
    scheduler.initialize()
    [item] = await scheduler.list_all()
    assert (item.vm_id, item.vm_kind, item.last_task_id) == ("desktop", None, "task-1")


@pytest.mark.parametrize("target", [{}, {"vm_kind": "unknown"}, {"vm_kind": "qemu", "vm_id": "vm"}])
def test_schedule_rejects_ambiguous_targets(target):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CreateSchedule(instruction="確認", run_at=200, **target)


async def test_disposable_runs_use_fresh_workspaces_and_keep_history(tmp_path, monkeypatch):
    from ai_desktop_agent.agent.llm.mock import MockLLMProvider
    from ai_desktop_agent.server.session import TaskSession
    from ai_desktop_agent.server.store import TaskStore
    from ai_desktop_agent.server.vm_creations import CreationStore, VmCreationService
    from ai_desktop_agent.server.vm_pool import VmPool
    from ai_desktop_agent.vm.fake import FakeDisplayBackend
    from tests.test_vm_pool import _FakeDocker

    docker = _FakeDocker()
    pool = VmPool(
        client=docker, state_file=tmp_path / "vms.json", overlays_dir=str(tmp_path / "vm")
    )
    service = VmCreationService(pool, CreationStore(tmp_path / "requests"))
    monkeypatch.setattr(server, "_pool", pool)
    monkeypatch.setattr(server, "_vm_creations", service)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_active_session", None)
    store = TaskStore(tmp_path)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_connect_vm_display", lambda _: FakeDisplayBackend())
    sessions = []
    release = asyncio.Event()

    def create(display):
        llm = MockLLMProvider()
        understand = llm.understand_instruction

        async def wait_for_release(*args, **kwargs):
            await release.wait()
            return await understand(*args, **kwargs)

        llm.understand_instruction = wait_for_release
        session = TaskSession(llm=llm, display=display, store=store, decision=None)
        sessions.append(session)
        return session

    monkeypatch.setattr(server, "_create_session", create)
    now = 100
    monkeypatch.setattr(schedules, "time", SimpleNamespace(time=lambda: now))

    def start_scheduler():
        scheduler = Scheduler(
            server._submit_scheduled_task, tmp_path, retire=server._retire_scheduled_environment
        )
        scheduler.initialize()
        monkeypatch.setattr(server.app.state, "scheduler", scheduler, raising=False)
        return scheduler

    scheduler = start_scheduler()
    item = await scheduler.add(
        CreateSchedule(vm_kind="container", instruction="確認", run_at=200, interval=86400)
    )
    vm_ids = set()
    try:
        for due in [200, 200 + 86400]:
            now = due
            release.clear()
            await scheduler.dispatch_due()
            run = await scheduler.reserve_environment(item.model_copy(update={"next_run": due}))
            receipt = await service.wait("creation-" + server.key_digest(run.creation_key))
            assert receipt.state == "succeeded"
            assert run.vm_id not in vm_ids
            vm_ids.add(run.vm_id)
            container = docker.containers.list()[0]
            container.attrs["State"]["Health"] = {"Status": "starting"}
            await scheduler.dispatch_due()
            [pending] = await scheduler.list_all()
            assert pending.next_run == due and pending.preparing
            with pytest.raises(HTTPException, match="予約タスク専用"):
                await server.create_task(
                    server.CreateTaskRequest(vm_id=run.vm_id, instruction="別の指示")
                )
            container.attrs["State"]["Health"]["Status"] = "healthy"
            await scheduler.dispatch_due()
            [saved] = await scheduler.list_all()
            assert saved.last_error is None
            assert saved.next_run == due + 86400
            # A new scheduler can recover cleanup even after removing the schedule itself.
            scheduler = start_scheduler()
            if due != 200:
                await scheduler.remove(item.id)
            await scheduler.retire_finished()
            assert not container.removed and docker.volumes.items
            release.set()
            await asyncio.wait_for(sessions[-1]._task, 5)
            # A temporary daemon outage must leave the allocation available for the next sweep.
            with monkeypatch.context() as outage:

                def unavailable(*args):
                    raise OSError("Docker unavailable")

                outage.setattr(pool, "remove_scheduled_vm", unavailable)
                await scheduler.retire_finished()
                assert await scheduler.environment_owner(run.vm_id)
            await scheduler.retire_finished()
            assert container.removed and not docker.volumes.items
            assert store.load(saved.last_task_id).state == "completed"
            assert await scheduler.environment_owner(run.vm_id) is None
    finally:
        release.set()
        await service.close()
