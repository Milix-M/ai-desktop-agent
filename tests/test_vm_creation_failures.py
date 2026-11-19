"""Durable queue transitions remain usable under failure and shutdown races."""

import asyncio
import errno
import json
import threading

import pytest

from ai_desktop_agent.server.kvm import KvmUnavailableError
from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_creations import CreationStore, VmCreationService
from ai_desktop_agent.server.vm_pool import LABEL_VM_ID, VmInfo
from ai_desktop_agent.vm.async_io import VmCreationBusyError, reserve_vm_creation_slot
from tests.test_vm_creations import KEY, receipt, terminal
from tests.test_vm_creations import managed as managed


@pytest.mark.parametrize(
    "vm_id",
    ["vm-" + "a" * 24, "desk-" + "a" * 23, "desk-../../outside", "desk-" + "Ａ" * 24],
    ids=["wrong-kind", "short", "path", "unicode"],
)
async def test_invalid_explicit_creation_ids_do_not_allocate_resources_and_allow_correction(
    managed, tmp_path, vm_id
):
    _, pool, client = managed
    volumes, networks = set(client.volumes.items), set(client.networks.items)
    with pytest.raises(ValueError, match="環境のID"):
        pool.create_vm(name="作業環境", kind="container", vm_id=vm_id)
    assert not (tmp_path / "vms.json").exists() and client.containers.runs == []
    assert set(client.volumes.items) == volumes and set(client.networks.items) == networks
    valid = "desk-" + "b" * 24
    created = pool.create_vm(name="作業環境", kind="container", vm_id=valid)
    assert created.id == valid and len(client.containers.runs) == 1
    assert set(json.loads((tmp_path / "vms.json").read_text())) == {valid}


@pytest.mark.parametrize("container_removed", [False, True])
async def test_an_allocated_id_cannot_overwrite_an_environment_or_its_retained_storage(
    managed, tmp_path, container_removed
):
    service, pool, client = managed
    accepted = await terminal(service)
    assert accepted.state == "succeeded"
    if container_removed:
        containers = client.containers.list(filters={"label": f"{LABEL_VM_ID}={accepted.vm_id}"})
        assert len(containers) == 1
        containers[0].remove(force=True)
        assert pool.get_vm(accepted.vm_id) is None
    path = tmp_path / "vms.json"
    saved = path.read_bytes()
    volumes, networks = set(client.volumes.items), set(client.networks.items)
    starts = len(client.containers.runs)
    with pytest.raises(ValueError, match="同じID"):
        pool.create_vm(name="上書きする環境", kind="container", vm_id=accepted.vm_id)
    assert path.read_bytes() == saved and len(client.containers.runs) == starts
    assert set(client.volumes.items) == volumes and set(client.networks.items) == networks
    if not container_removed:
        assert pool.get_vm(accepted.vm_id).name == accepted.result.name


@pytest.mark.parametrize(
    "error,status",
    [
        (KvmUnavailableError(), 409),
        (DockerUnavailableError("daemon offline"), 503),
        (ValueError("invalid environment configuration"), 400),
        (RuntimeError("internal detail"), 503),
    ],
)
async def test_an_operation_failure_is_durable_and_does_not_stop_the_next_job(
    managed, monkeypatch, error, status
):
    service, pool, client = managed
    create = pool.create_vm
    attempts = []

    def fail_once(*args, **kwargs):
        attempts.append(kwargs["vm_id"])
        if len(attempts) == 1:
            raise error
        return create(*args, **kwargs)

    monkeypatch.setattr(pool, "create_vm", fail_once)
    failed = await terminal(service)
    assert failed.state == "failed" and failed.error_status == status and failed.error
    if isinstance(error, RuntimeError) and not isinstance(error, DockerUnavailableError):
        assert "internal detail" not in failed.error
    assert (await terminal(service)).id == failed.id and len(attempts) == 1
    completed = await terminal(service, key=KEY + "-next")
    assert completed.state == "succeeded" and len(client.containers.runs) == 1
    assert failed.vm_id != completed.vm_id


@pytest.mark.parametrize("error_type", [ValueError, DockerUnavailableError])
@pytest.mark.parametrize(
    "message",
    ["", "   ", "x" * 2049, "保存先の状態" * 600, "bad\ud800text"],
    ids=["empty", "blank", "ascii-long", "unicode-long", "invalid-unicode"],
)
async def test_an_empty_or_large_error_cannot_poison_the_creation_queue(
    managed, monkeypatch, error_type, message
):
    service, pool, client = managed
    create = pool.create_vm
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error_type(message)
        return create(*args, **kwargs)

    monkeypatch.setattr(pool, "create_vm", fail_once)
    result = await terminal(service)
    assert result.state == "failed" and 0 < len(result.error) <= 2048 and result.error.strip()
    result.error.encode("utf-8")
    assert result.error_status == (400 if error_type is ValueError else 503)
    assert (await terminal(service, key=KEY + "-next")).state == "succeeded"
    assert len(client.containers.runs) == 1


@pytest.mark.parametrize(
    "damage",
    [
        "fingerprint",
        "time",
        "unfinished_result",
        "failed_no_message",
        "failed_no_status",
        "queued_error",
        "queued_status",
        "success_without_result",
        "success_wrong_id",
        "success_wrong_kind",
        "success_unmanaged",
        "success_with_error",
        "success_with_status",
    ],
)
async def test_inconsistent_receipt_states_do_not_recover_or_overwrite(managed, damage):
    service, _, client = managed
    saved = receipt()
    service.store.write(saved)
    path = service.store._path(saved.id)
    data = json.loads(path.read_text())
    result = VmInfo(saved.vm_id, saved.name, "running", kind=saved.kind).to_dict()
    if damage.startswith("success_"):
        data.update(state="succeeded", result=result)
        if damage == "success_without_result":
            data["result"] = None
        elif damage == "success_wrong_id":
            result["id"] = "desk-other"
        elif damage == "success_wrong_kind":
            result["kind"] = "qemu"
        elif damage == "success_unmanaged":
            result["managed"] = False
        elif damage == "success_with_error":
            data["error"] = "failure"
        else:
            data["error_status"] = 503
    elif damage == "fingerprint":
        data["name"] = "different request"
    elif damage == "time":
        data["updated_at"] = 0.0
    elif damage == "unfinished_result":
        data["result"] = result
    elif damage == "failed_no_message":
        data.update(state="failed", error_status=503)
    elif damage == "failed_no_status":
        data.update(state="failed", error="failure")
    elif damage == "queued_error":
        data["error"] = "failure"
    else:
        data["error_status"] = 503
    damaged = json.dumps(data)
    path.write_text(damaged)
    with pytest.raises(StorageError):
        await service.get(saved.id)
    assert client.containers.runs == [] and path.read_text() == damaged


def test_receipt_filename_must_match_the_validated_record(tmp_path):
    store = CreationStore(tmp_path)
    first, second = receipt(), receipt(KEY + "-second")
    path = store._path(first.id)
    path.write_text(second.model_dump_json())
    with pytest.raises(StorageError):
        store.read(first.id)
    assert path.read_text() == second.model_dump_json()


def test_invalid_internal_updates_are_rejected_without_replacing_a_valid_receipt(tmp_path):
    store = CreationStore(tmp_path)
    original = receipt()
    store.write(original)
    path = store._path(original.id)
    saved = path.read_bytes()
    with pytest.raises(StorageError):
        store.write(original.updated(state="failed"))
    assert path.read_bytes() == saved


def test_invalid_receipt_filenames_stop_recovery(tmp_path):
    store = CreationStore(tmp_path)
    (tmp_path / "creation-invalid.json").write_text(receipt().model_dump_json())
    with pytest.raises(StorageError):
        store.incomplete()


def test_a_receipt_removed_during_the_directory_scan_is_not_rescheduled(tmp_path, monkeypatch):
    store = CreationStore(tmp_path)
    saved = receipt()
    store.write(saved)
    target = store._path(saved.id)
    open_file = type(target).open

    def removed(path, *args, **kwargs):
        if path == target:
            path.unlink(missing_ok=True)
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(type(target), "open", removed)
    assert store.incomplete() == []
    assert not target.exists()


def test_owner_lock_os_failure_closes_the_handle_and_allows_repair(tmp_path, monkeypatch):
    from ai_desktop_agent.server import vm_creations

    store = CreationStore(tmp_path / "requests")
    handles = []
    fdopen, flock = vm_creations.os.fdopen, vm_creations.fcntl.flock

    def opened(*args, **kwargs):
        handle = fdopen(*args, **kwargs)
        handles.append(handle)
        return handle

    def fail(*_):
        raise OSError(errno.EIO, "lock I/O failure")

    monkeypatch.setattr(vm_creations.os, "fdopen", opened)
    monkeypatch.setattr(vm_creations.fcntl, "flock", fail)
    with pytest.raises(StorageError):
        store.acquire_owner()
    assert len(handles) == 1 and handles[0].closed
    monkeypatch.setattr(vm_creations.fcntl, "flock", flock)
    owner = store.acquire_owner()
    owner.close()


def test_owner_directory_failure_preserves_data_and_can_be_repaired(tmp_path):
    directory = tmp_path / "requests"
    directory.write_text("keep existing data")
    store = CreationStore(directory)
    with pytest.raises(StorageError):
        store.acquire_owner()
    assert directory.read_text() == "keep existing data"
    directory.unlink()
    owner = store.acquire_owner()
    owner.close()


async def test_recovery_releases_partial_admission_when_legacy_work_owns_capacity(managed):
    service, _, client = managed
    records = [receipt(KEY + f"-saved-{i}") for i in range(2)]
    for record in records:
        service.store.write(record)
    held = [reserve_vm_creation_slot() for _ in range(7)]
    try:
        with pytest.raises(VmCreationBusyError):
            await service.start()
        assert client.containers.runs == []
    finally:
        for slot in held:
            slot.release()
    await service.start()
    for record in records:
        assert (await service.wait(record.id)).state == "succeeded"
    assert len(client.containers.runs) == 2


async def test_recovery_rejects_an_overfull_saved_queue_without_starting_work(managed):
    service, _, client = managed
    for i in range(9):
        service.store.write(receipt(KEY + f"-saved-{i}"))
    previous = {p.name: p.read_bytes() for p in service.store.directory.glob("*.json")}
    with pytest.raises(StorageError):
        await service.start()
    assert client.containers.runs == []
    assert {p.name: p.read_bytes() for p in service.store.directory.glob("*.json")} == previous


async def test_shutdown_during_the_running_receipt_write_stops_before_docker(managed, monkeypatch):
    service, _, client = managed
    entered, release = threading.Event(), threading.Event()
    write = service.store.write

    def blocked(record):
        if record.state == "running":
            entered.set()
            assert release.wait(5)
        write(record)

    monkeypatch.setattr(service.store, "write", blocked)
    accepted = await service.submit(KEY, "作業環境", "container")
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(service.close())
        async with asyncio.timeout(2):
            while not service._closing:
                await asyncio.sleep(0)
    finally:
        release.set()
        if closing is not None:
            await closing
    assert service.store.read(accepted.id).state == "failed"
    assert client.containers.runs == []


async def test_shutdown_waits_for_an_owned_acceptance_and_releases_its_lease(managed, monkeypatch):
    service, _, client = managed
    entered, release = threading.Event(), threading.Event()
    write = service.store.write

    def blocked(record):
        if record.state == "queued":
            entered.set()
            assert release.wait(5)
        write(record)

    monkeypatch.setattr(service.store, "write", blocked)
    submission = asyncio.create_task(service.submit(KEY, "作業環境", "container"))
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(service.close())
        async with asyncio.timeout(2):
            while not service._closing:
                await asyncio.sleep(0)
        assert not closing.done()
    finally:
        release.set()
        accepted = await submission
        if closing is not None:
            await closing
    assert service.store.read(accepted.id).state == "failed" and client.containers.runs == []
    owner = service.store.acquire_owner()
    owner.close()
    with pytest.raises(DockerUnavailableError):
        await service.start()
    with pytest.raises(DockerUnavailableError):
        await service.acquire_ownership()


async def test_storage_failure_preserves_queued_work_and_reconciles_accepted_side_effects(
    managed, monkeypatch
):
    service, pool, client = managed
    entered, release = threading.Event(), threading.Event()
    create = pool.create_vm
    first_call = True

    def uncertain(*args, **kwargs):
        nonlocal first_call
        if not first_call:
            return create(*args, **kwargs)
        first_call = False
        create(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        raise StorageError("durability became uncertain after the container was accepted")

    monkeypatch.setattr(pool, "create_vm", uncertain)
    first = await service.submit(KEY, "作業環境", "container")
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        second = await service.submit(KEY + "-second", "作業環境", "container")
    finally:
        release.set()
    with pytest.raises(StorageError):
        await service.wait(first.id)
    with pytest.raises(StorageError):
        await service.wait(second.id)
    assert len(client.containers.runs) == 1
    await service.close()
    assert service.store.read(first.id).state == "running"
    assert service.store.read(second.id).state == "queued"
    recovered = VmCreationService(pool, CreationStore(service.store.directory))
    try:
        await recovered.start()
        assert (await recovered.wait(first.id)).state == "succeeded"
        assert (await recovered.wait(second.id)).state == "succeeded"
        assert len(client.containers.runs) == 2
    finally:
        await recovered.close()


@pytest.mark.parametrize("route,key", [("/vms", None), ("/vms", KEY), ("/vm-creations", KEY)])
async def test_invalid_unicode_environment_names_are_rejected_before_acceptance(
    managed, monkeypatch, route, key
):
    from httpx import ASGITransport

    from ai_desktop_agent.server import app as server
    from tests.http import AuthenticatedClient

    service, pool, client = managed
    monkeypatch.setattr(server, "get_pool", lambda: pool)
    monkeypatch.setattr(server, "_vm_creations", service)
    headers = {"Idempotency-Key": key} if key else {}
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app, raise_app_exceptions=False), base_url="http://test"
    ) as api:
        result = await api.post(
            route,
            content=json.dumps({"name": "bad\ud800name", "kind": "container"}),
            headers={**headers, "Content-Type": "application/json"},
        )
    assert result.status_code == 422, result.text
    assert client.containers.runs == []
    assert not list(service.store.directory.glob("creation-*.json"))


@pytest.mark.parametrize(
    "name", ["bad\ud800name", 99, "n" * 129], ids=["unicode", "type", "length"]
)
@pytest.mark.parametrize("target", ["service", "pool"])
async def test_invalid_programmatic_names_do_not_acquire_or_create(managed, target, name):
    service, pool, client = managed
    with pytest.raises(ValueError):
        if target == "service":
            await service.submit(KEY, name, "container")
        else:
            pool.create_vm(name=name, kind="container")
    assert client.containers.runs == []
    assert not list(service.store.directory.glob("creation-*.json"))
    assert service._owner is None


async def test_ownership_can_be_retried_after_a_real_directory_repair(managed):
    service, _, client = managed
    directory = service.store.directory
    directory.write_text("pre-existing file")
    with pytest.raises(StorageError):
        await service.acquire_ownership()
    assert directory.read_text() == "pre-existing file"
    directory.unlink()
    await service.acquire_ownership()
    await service.start()
    assert (await terminal(service)).state == "succeeded"
    assert len(client.containers.runs) == 1


async def test_shutdown_save_failure_leaves_unstarted_work_recoverable(managed, monkeypatch):
    service, pool, client = managed
    entered, release = threading.Event(), threading.Event()
    create, write = pool.create_vm, service.store.write
    first_call = True

    def blocked(*args, **kwargs):
        nonlocal first_call
        if first_call:
            first_call = False
            entered.set()
            assert release.wait(5)
        return create(*args, **kwargs)

    def unavailable(record):
        if record.state == "failed":
            raise StorageError("shutdown write is unavailable")
        write(record)

    monkeypatch.setattr(pool, "create_vm", blocked)
    monkeypatch.setattr(service.store, "write", unavailable)
    first = await service.submit(KEY, "作業環境", "container")
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        second = await service.submit(KEY + "-second", "作業環境", "container")
        closing = asyncio.create_task(service.close())
        async with asyncio.timeout(2):
            while second.id in service._pending:
                await asyncio.sleep(0)
    finally:
        release.set()
        if closing is not None:
            await closing
    assert service.store.read(first.id).state == "succeeded"
    assert service.store.read(second.id).state == "queued"
    recovered = VmCreationService(pool, CreationStore(service.store.directory))
    try:
        await recovered.start()
        assert (await recovered.wait(second.id)).state == "succeeded"
        assert len(client.containers.runs) == 2
    finally:
        await recovered.close()
