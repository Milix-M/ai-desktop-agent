import asyncio
import hashlib
import json
import threading

import pytest

from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_creations import (
    CreationConflictError,
    CreationRecord,
    CreationStore,
    VmCreationService,
    _fingerprint,
)
from ai_desktop_agent.server.vm_pool import ID_PREFIX, VmPool
from ai_desktop_agent.vm.async_io import VmCreationBusyError
from tests.test_vm_pool import _FakeDocker

KEY = "create-retry-0123456789abcdef"


def receipt(key=KEY, state="queued", kind="container"):
    digest = hashlib.sha256(key.encode()).hexdigest()
    return CreationRecord(
        id="creation-" + digest,
        fingerprint=_fingerprint("作業環境", kind),
        vm_id=f"{ID_PREFIX[kind]}-{digest[:24]}",
        name="作業環境",
        kind=kind,
        state=state,
        created_at=1.0,
        updated_at=1.0,
    )


@pytest.fixture
async def managed(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "ai-desktop-agent")
    monkeypatch.setenv("USE_KVM", "true")
    monkeypatch.setenv("ALLOW_TCG_VM", "false")
    monkeypatch.setenv("VM_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("VM_IMAGE_REF", "test-vm-image:latest")
    pool = VmPool(
        client=_FakeDocker(),
        state_file=tmp_path / "vms.json",
        overlays_dir=str(tmp_path / "vm/overlays"),
    )
    service = VmCreationService(pool, CreationStore(tmp_path / "requests"))
    yield service, pool, pool._client
    await service.close()


async def terminal(service, key=KEY, name="作業環境", kind="container"):
    record = await service.submit(key, name, kind)
    async with asyncio.timeout(5):
        result = await service.wait(record.id)
    return result


async def test_concurrent_and_repeated_retries_create_one_environment(managed):
    service, _, client = managed
    accepted = await asyncio.gather(
        *[service.submit(KEY, "作業環境", "container") for _ in range(8)]
    )
    assert len({record.id for record in accepted}) == 1
    result = await terminal(service)
    assert result.state == "succeeded", result
    assert result.result.id == accepted[0].vm_id
    assert len(client.containers.runs) == 1
    assert (await terminal(service)).vm_id == result.vm_id
    assert len(client.containers.runs) == 1
    assert KEY not in service.store._path(result.id).read_text()


async def test_retry_cannot_change_the_creation_payload(managed):
    service, _, client = managed
    await terminal(service)
    with pytest.raises(CreationConflictError):
        await service.submit(KEY, "別の環境", "container")
    with pytest.raises(CreationConflictError):
        await service.submit(KEY, "作業環境", "qemu")
    assert len(client.containers.runs) == 1


async def test_retry_after_deletion_does_not_recreate_the_environment(managed):
    service, pool, client = managed
    result = await terminal(service)
    assert pool.remove_vm(result.vm_id)
    assert (await terminal(service)).vm_id == result.vm_id
    assert pool.get_vm(result.vm_id) is None
    assert len(client.containers.runs) == 1


async def test_caller_cancellation_during_acceptance_does_not_lose_the_job(managed, monkeypatch):
    service, _, client = managed
    entered, release = threading.Event(), threading.Event()
    original = service.store.write

    def write(record):
        if record.state == "queued":
            entered.set()
            assert release.wait(5)
        original(record)

    monkeypatch.setattr(service.store, "write", write)
    submission = asyncio.create_task(service.submit(KEY, "作業環境", "container"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        submission.cancel()
        with pytest.raises(asyncio.CancelledError):
            await submission
    finally:
        release.set()
    result = await terminal(service)
    assert result.state == "succeeded"
    assert len(client.containers.runs) == 1


@pytest.mark.parametrize("state", ["queued", "running"])
async def test_recovery_distinguishes_unstarted_from_uncertain_creations(managed, state):
    service, _, client = managed
    saved = receipt(state=state)
    service.store.write(saved)
    await service.start()
    result = await service.wait(saved.id)
    if state == "queued":
        assert result.state == "succeeded"
        assert len(client.containers.runs) == 1
    else:
        assert result.state == "failed" and result.error_status == 503
        assert len(client.containers.runs) == 0
        assert (await terminal(service)).state == "failed"
        assert len(client.containers.runs) == 0


async def test_recovery_finds_a_created_environment_without_repeating_docker(managed):
    service, pool, client = managed
    saved = receipt(state="running")
    pool.create_vm(saved.name, saved.kind, vm_id=saved.vm_id)
    service.store.write(saved)
    result = await terminal(service)
    assert result.state == "succeeded"
    assert len(client.containers.runs) == 1


async def test_lost_terminal_write_is_reconciled_after_service_restart(managed, monkeypatch):
    service, pool, client = managed
    original = service.store.write

    def write(record):
        if record.state == "succeeded":
            raise StorageError("controlled storage failure")
        original(record)

    monkeypatch.setattr(service.store, "write", write)
    record = await service.submit(KEY, "作業環境", "container")
    with pytest.raises(StorageError):
        await service.wait(record.id)
    with pytest.raises(StorageError):
        await service.submit(KEY, "作業環境", "container")
    assert pool.get_vm(record.vm_id) is not None
    assert len(client.containers.runs) == 1
    await service.close()
    recovered = VmCreationService(pool, CreationStore(service.store.directory))
    try:
        result = await terminal(recovered)
        assert result.state == "succeeded"
        assert len(client.containers.runs) == 1
    finally:
        await recovered.close()


async def test_store_has_one_active_coordinator(managed):
    service, pool, _ = managed
    await service.start()
    other = VmCreationService(pool, CreationStore(service.store.directory))
    try:
        with pytest.raises(DockerUnavailableError, match="別のバックエンド"):
            await other.start()
    finally:
        await other.close()


@pytest.mark.parametrize("damage", ["json", "target", "result", "large", "symlink"])
async def test_corrupt_receipts_stop_creation_instead_of_replaying(managed, damage, tmp_path):
    service, _, client = managed
    saved = receipt()
    service.store.write(saved)
    path = service.store._path(saved.id)
    if damage == "json":
        path.write_text("{")
    elif damage == "large":
        path.write_text(" " * (64 * 1024 + 1))
    elif damage == "symlink":
        target = tmp_path / "other.json"
        target.write_text(saved.model_dump_json())
        path.unlink()
        path.symlink_to(target)
    else:
        data = json.loads(path.read_text())
        data["vm_id" if damage == "target" else "result"] = "different"
        path.write_text(json.dumps(data))
    with pytest.raises(StorageError):
        await service.submit(KEY, "作業環境", "container")
    assert len(client.containers.runs) == 0


async def test_close_finishes_active_creation_and_stops_unstarted_jobs(managed, monkeypatch):
    service, pool, client = managed
    entered, release = threading.Event(), threading.Event()

    def copy(vm_id, *, image=None):
        assert image is not None
        entered.set()
        assert release.wait(5)
        return "/host/vm/overlays/" + vm_id + "/disk.qcow2"

    monkeypatch.setattr(pool, "_create_overlay", copy)
    first = await service.submit(KEY, "作業環境", "qemu")
    assert await asyncio.to_thread(entered.wait, 2)
    second = await service.submit(KEY + "-second", "別の環境", "container")
    closing = asyncio.create_task(service.close())
    try:
        await asyncio.sleep(0.05)
        assert not closing.done()
    finally:
        release.set()
        await closing
    assert service.store.read(first.id).state == "succeeded"
    assert service.store.read(second.id).state == "failed"
    assert len(client.containers.runs) == 1


async def test_queue_limit_counts_only_new_receipts(managed, monkeypatch):
    service, pool, _ = managed
    entered, release = threading.Event(), threading.Event()

    def copy(vm_id, *, image=None):
        assert image is not None
        entered.set()
        assert release.wait(5)
        return "/host/vm/overlays/" + vm_id + "/disk.qcow2"

    monkeypatch.setattr(pool, "_create_overlay", copy)
    first = await service.submit(KEY, "作業環境", "qemu")
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for i in range(7):
            await service.submit(KEY + f"-{i}", "別の環境", "container")
        assert (await service.submit(KEY, "作業環境", "qemu")).id == first.id
        with pytest.raises(VmCreationBusyError):
            await service.submit(KEY + "-overflow", "別の環境", "container")
    finally:
        release.set()


async def test_a_published_queued_receipt_is_scheduled_after_an_uncertain_save(
    managed, monkeypatch
):
    service, _, client = managed
    original = service.store.write
    failed = False

    def write(record):
        nonlocal failed
        original(record)
        if record.state == "queued" and not failed:
            failed = True
            raise StorageError("directory fsync failed after replace")

    monkeypatch.setattr(service.store, "write", write)
    with pytest.raises(StorageError):
        await service.submit(KEY, "作業環境", "container")
    assert client.containers.runs == []
    assert (await terminal(service)).state == "succeeded"
    assert len(client.containers.runs) == 1


async def test_recovery_retries_a_daemon_error_without_scheduling_queued_work_twice(
    managed, monkeypatch
):
    service, pool, client = managed
    queued, uncertain = sorted([receipt(KEY), receipt(KEY + "-other")], key=lambda item: item.id)
    uncertain = uncertain.updated(state="running")
    service.store.write(queued)
    service.store.write(uncertain)
    original = pool.remove_vm
    attempts = 0

    def remove(vm_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DockerUnavailableError("controlled daemon outage")
        return original(vm_id)

    monkeypatch.setattr(pool, "remove_vm", remove)
    with pytest.raises(DockerUnavailableError):
        await service.start()
    assert client.containers.runs == []
    assert service._pending == {}
    await service.start()
    assert (await service.wait(queued.id)).state == "succeeded"
    assert service.store.read(uncertain.id).state == "failed"
    assert len(client.containers.runs) == 1


async def test_cancelled_shutdown_still_drains_the_worker_and_releases_ownership(
    managed, monkeypatch
):
    service, pool, client = managed
    entered, release = threading.Event(), threading.Event()

    def copy(vm_id, *, image=None):
        assert image is not None
        entered.set()
        assert release.wait(5)
        return "/host/vm/overlays/" + vm_id + "/disk.qcow2"

    monkeypatch.setattr(pool, "_create_overlay", copy)
    accepted = await service.submit(KEY, "作業環境", "qemu")
    assert await asyncio.to_thread(entered.wait, 2)
    closing = asyncio.create_task(service.close())
    try:
        async with asyncio.timeout(2):
            while not service._closing:
                await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
    finally:
        release.set()
        await service.close()
    assert service.store.read(accepted.id).state == "succeeded"
    assert len(client.containers.runs) == 1
    owner = service.store.acquire_owner()
    owner.close()


@pytest.mark.parametrize(
    "field, value",
    [
        ("version", True),
        ("name", 99),
        ("vnc_port", True),
        ("extra", "unexpected"),
    ],
)
async def test_completed_receipt_schema_remains_strict(managed, field, value):
    service, _, _ = managed
    completed = await terminal(service)
    path = service.store._path(completed.id)
    data = json.loads(path.read_text())
    if field == "version":
        data[field] = value
    else:
        data["result"][field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(StorageError):
        await service.get(completed.id)


async def test_a_delayed_queued_read_cannot_reenqueue_a_completed_creation(managed, monkeypatch):
    service, _, client = managed
    running_entered, release_running = threading.Event(), threading.Event()
    read_entered, release_read = threading.Event(), threading.Event()
    completed_write = threading.Event()
    original_write, original_read = service.store.write, service.store.read
    delayed = False

    def write(record):
        if record.state == "running":
            running_entered.set()
            assert release_running.wait(5)
        original_write(record)
        if record.state == "succeeded":
            completed_write.set()

    def read(job_id):
        nonlocal delayed
        record = original_read(job_id)
        if record is not None and record.state == "queued" and not delayed:
            delayed = True
            read_entered.set()
            assert release_read.wait(5)
        return record

    monkeypatch.setattr(service.store, "write", write)
    monkeypatch.setattr(service.store, "read", read)
    accepted = await service.submit(KEY, "作業環境", "container")
    assert await asyncio.to_thread(running_entered.wait, 2)
    retry = asyncio.create_task(service.submit(KEY, "作業環境", "container"))
    try:
        assert await asyncio.to_thread(read_entered.wait, 2)
        release_running.set()
        if await asyncio.to_thread(completed_write.wait, 0.3):
            async with asyncio.timeout(2):
                while accepted.id in service._pending:
                    await asyncio.sleep(0)
    finally:
        release_running.set()
        release_read.set()
        await retry
    finished = await service.wait(accepted.id)
    assert finished.state == "succeeded", finished
    assert len(client.containers.runs) == 1


@pytest.mark.parametrize("kind", ["qemu", "container", "android"])
async def test_disposable_receipt_owns_only_its_allocation(managed, kind, tmp_path, monkeypatch):
    service, pool, client = managed
    iso = tmp_path / "android.iso"
    iso.write_bytes(b"iso")
    monkeypatch.setenv("ANDROID_ISO_HOST", str(iso))
    monkeypatch.setenv("ANDROID_IMAGE_REF", "test-android-image")
    accepted = await service.submit(KEY, "使い捨て", kind, disposable=True)
    finished = await service.wait(accepted.id)
    assert finished.state == "succeeded", finished.error
    identity = pool.get_scheduled_instance(finished.vm_id, finished.id)
    assert identity.info.kind == kind
    assert pool.get_scheduled_instance(finished.vm_id, "unrelated") is None
    pool.remove_scheduled_vm(finished.vm_id, identity.container_id, "unrelated")
    pool.remove_scheduled_vm(finished.vm_id, "replaced-container-id", finished.id)
    assert pool.get_vm(finished.vm_id) is not None
    pool.remove_scheduled_vm(finished.vm_id, identity.container_id, finished.id)
    assert pool.get_vm(finished.vm_id) is None
    assert not client.volumes.items


async def test_disposable_creation_recovery_leaves_an_unrelated_vm_alone(managed):
    service, pool, _ = managed
    saved = receipt(state="running").model_copy(update={"disposable": True})
    pool.create_vm(name="既存環境", vm_id=saved.vm_id, kind=saved.kind)
    service.store.write(saved)
    await service.start()
    assert (await service.get(saved.id)).state == "failed"
    assert pool.get_vm(saved.vm_id).name == "既存環境"


@pytest.mark.parametrize("delete_while_booting", [True, False])
async def test_disposable_schedule_reclaims_an_unused_environment(
    managed, tmp_path, monkeypatch, delete_while_booting
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from ai_desktop_agent.server import app as server
    from ai_desktop_agent.server import schedules

    service, pool, client = managed
    now = 100
    monkeypatch.setattr(schedules, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(server, "_pool", pool)
    monkeypatch.setattr(server, "_vm_creations", service)
    monkeypatch.setattr(server, "_sessions", {})
    submit = AsyncMock(side_effect=HTTPException(502, "VNC接続失敗"))
    monkeypatch.setattr(server, "create_task", submit)
    scheduler = schedules.Scheduler(
        server._submit_scheduled_task, tmp_path, retire=server._retire_scheduled_environment
    )
    scheduler.initialize()
    monkeypatch.setattr(server.app.state, "scheduler", scheduler, raising=False)
    item = await scheduler.add(
        schedules.CreateSchedule(vm_kind="container", instruction="確認", run_at=200)
    )
    now = 200
    await scheduler.dispatch_due()
    run = await scheduler.reserve_environment(item)
    if delete_while_booting:
        await scheduler.remove(item.id)
    receipt = await service.wait("creation-" + server.key_digest(run.creation_key))
    assert receipt.state == "succeeded"
    await scheduler.dispatch_due()
    if delete_while_booting:
        submit.assert_not_called()
    else:
        [finished] = await scheduler.list_all()
        assert finished.last_error == "VNC接続失敗" and finished.next_run is None
    await scheduler.retire_finished()
    assert not client.volumes.items
    assert pool.get_vm(run.vm_id) is None
