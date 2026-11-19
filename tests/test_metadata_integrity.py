"""Ambiguous durable JSON must not select a state, owner, or recovery action."""

import json

import pytest

from ai_desktop_agent.server.android_provision import AndroidProvisioner
from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.store import TaskRecord, TaskStore
from ai_desktop_agent.server.vm_pool import VmInfo
from ai_desktop_agent.server.workspaces import HomeBackups
from tests.test_vm_creations import managed as managed
from tests.test_vm_creations import receipt
from tests.test_vm_pool import _FakeDocker
from tests.test_workspaces import archive_bytes, source_for


def duplicate_object(payload, key, first_value, *, escaped=False):
    spelling = key if not escaped else f"\\u{ord(key[0]):04x}{key[1:]}"
    assert payload.startswith("{")
    return '{"' + spelling + '":' + json.dumps(first_value, ensure_ascii=False) + "," + payload[1:]


@pytest.mark.parametrize("escaped", [False, True])
async def test_duplicate_creation_states_do_not_start_a_recovered_job(managed, escaped):
    service, _, client = managed
    saved = receipt()
    service.store.write(saved)
    path = service.store._path(saved.id)
    damaged = duplicate_object(path.read_text(), "state", "failed", escaped=escaped)
    path.write_text(damaged)
    with pytest.raises(StorageError):
        await service.start()
    assert client.containers.runs == [] and path.read_text() == damaged


@pytest.mark.parametrize("escaped", [False, True])
async def test_duplicate_nested_creation_results_cannot_be_accepted_as_managed(managed, escaped):
    service, _, client = managed
    saved = receipt()
    saved = saved.updated(
        state="succeeded",
        result=VmInfo(saved.vm_id, saved.name, "running", kind=saved.kind, managed=True),
    )
    service.store.write(saved)
    path = service.store._path(saved.id)
    data = json.loads(path.read_text())
    result = duplicate_object(json.dumps(data.pop("result")), "managed", False, escaped=escaped)
    damaged = json.dumps(data)[:-1] + ',"result":' + result + "}"
    path.write_text(damaged)
    with pytest.raises(StorageError):
        await service.get(saved.id)
    assert client.containers.runs == [] and path.read_text() == damaged


@pytest.mark.parametrize("nested", [False, True])
def test_duplicate_vm_allocations_are_not_selected_or_overwritten(tmp_path, nested):
    from ai_desktop_agent.server.vm_pool import VmPool

    path = tmp_path / "vms.json"
    payload = '{"desk-owned":{"name":"replacement"}}'
    damaged = (
        '{"desk-owned":{"name":"original","name":"replacement"}}'
        if nested
        else duplicate_object(payload, "desk-owned", {"name": "original"})
    )
    path.write_text(damaged)
    pool = VmPool(client=_FakeDocker(), state_file=path)
    with pytest.raises(StorageError):
        pool.list_vms()
    with pytest.raises(StorageError):
        pool._save_state("desk-new", {"name": "new"})
    assert path.read_text() == damaged


@pytest.mark.parametrize("nested", [False, True])
def test_ambiguous_history_is_skipped_without_rewriting_the_file(tmp_path, nested):
    store = TaskStore(tmp_path)
    record = TaskRecord(
        "saved-task", "報告書を保存する", "completed", execution_usage={"llm_calls": 2}
    )
    store.save(record)
    path = store._dir / "saved-task.json"
    if nested:
        data = json.loads(path.read_text())
        data.pop("execution_usage")
        damaged = json.dumps(data)[:-1] + ',"execution_usage":{"llm_calls":99,"llm_calls":2}}'
    else:
        damaged = duplicate_object(path.read_text(), "state", "executing")
    path.write_text(damaged)
    assert store.load(record.id) is None
    assert store.list() == []
    assert path.read_text() == damaged


def test_ambiguous_task_order_is_not_used_to_allocate_or_overwrite(tmp_path):
    store = TaskStore(tmp_path)
    damaged = '{"last_order":1,"last_order":99}'
    store._order_file.write_text(damaged)
    with pytest.raises(StorageError):
        store.reserve_session_order()
    assert store._order_file.read_text() == damaged


@pytest.mark.parametrize("nested", [False, True])
def test_ambiguous_android_setup_state_does_not_repeat_or_mark_setup(tmp_path, nested):
    path = tmp_path / "android-state.json"
    damaged = (
        '{"android":{"done":true,"done":false,"data_id":"known"}}'
        if nested
        else (
            '{"android":{"done":true,"data_id":"known"},"android":{"done":false,"data_id":"known"}}'
        )
    )
    path.write_text(damaged)
    provisioner = AndroidProvisioner(state_file=path)
    with pytest.raises(StorageError):
        provisioner.needs("android")
    with pytest.raises(StorageError):
        provisioner.mark_done("android", data_id="known")
    assert path.read_text() == damaged


@pytest.mark.parametrize(
    "field, first", [("project", "other-project"), ("vm_id", "other-vm"), ("bytes", 1)]
)
def test_ambiguous_backup_metadata_does_not_verify_or_change_the_archive(tmp_path, field, first):
    backups = HomeBackups(tmp_path, "my-project")
    name = backups.capture(source_for(archive_bytes()), "desktop")
    archive, _, _ = backups.verified(name)
    original = archive.read_bytes()
    metadata = archive.with_suffix(".json")
    damaged = duplicate_object(metadata.read_text(), field, first)
    metadata.write_text(damaged)
    with pytest.raises(StorageError):
        backups.verified(name)
    assert archive.read_bytes() == original and metadata.read_text() == damaged


@pytest.mark.parametrize("reader", ["vm", "android", "backup"])
def test_excessively_nested_metadata_is_reported_without_rewriting(tmp_path, reader):
    from ai_desktop_agent.server.vm_pool import VmPool

    damaged = '{"nested":' + "[" * 1100 + "0" + "]" * 1100 + "}"
    path = tmp_path / "metadata.json"
    if reader == "vm":
        pool = VmPool(client=_FakeDocker(), state_file=path)

        def operation():
            pool._save_state("desk-new", {"name": "new"})
    elif reader == "android":
        provisioner = AndroidProvisioner(state_file=path)

        def operation():
            provisioner.mark_done("android", data_id="known")
    else:
        backups = HomeBackups(tmp_path, "my-project")
        name = backups.capture(source_for(archive_bytes()), "desktop")
        path = backups.root / (name + ".json")

        def operation():
            backups.verified(name)

    path.write_text(damaged)
    with pytest.raises(StorageError):
        operation()
    assert path.read_text() == damaged
