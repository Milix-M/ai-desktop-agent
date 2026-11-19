"""Unknown metadata cannot bypass JSON safety or poison later durable writes."""

import json
from unittest.mock import Mock

import pytest

from ai_desktop_agent.server.android_provision import AndroidProvisioner
from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.store import TaskRecord, TaskStore
from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.server.workspaces import HomeBackups
from tests.test_android_service_lifecycle import target_pool
from tests.test_vm_pool import _FakeContainer, _FakeDocker
from tests.test_workspaces import archive_bytes, source_for

INVALID = [
    pytest.param("NaN", id="nan"),
    pytest.param("Infinity", id="positive-infinity"),
    pytest.param("-Infinity", id="negative-infinity"),
    pytest.param("1e999", id="overflow"),
    pytest.param(r'"\ud800"', id="invalid-text"),
    pytest.param(r'{"\ud800":"unsafe-key"}', id="invalid-key"),
    pytest.param(r'[[0,"\udfff"]]', id="invalid-array-text"),
]


def extension(payload, fragment):
    assert payload.endswith("}")
    return payload[:-1] + ',"future_extension":[{"child":' + fragment + "}]}"


@pytest.mark.parametrize("fragment", INVALID)
def test_invalid_extension_cannot_start_android_setup_or_replace_its_record(tmp_path, fragment):
    state = tmp_path / "state.json"
    other = extension('{"done":true,"data_id":"known"}', fragment)
    damaged = '{"and-target":{"done":false,"data_id":"pending"},"and-other":' + other + "}"
    state.write_text(damaged)
    pool = target_pool()
    setup = AndroidProvisioner(pool=pool, state_file=state)
    configure = Mock(return_value=True)
    setup.provision_vm = configure
    with pytest.raises(StorageError):
        setup.ensure()
    configure.assert_not_called()
    pool.prepare_vm.assert_not_called()
    assert state.read_text() == damaged


@pytest.mark.parametrize("fragment", INVALID)
def test_invalid_allocation_extension_cannot_stop_a_container_or_be_overwritten(tmp_path, fragment):
    state = tmp_path / "vms.json"
    damaged = '{"desk-owned":' + extension('{"name":"retained"}', fragment) + "}"
    state.write_text(damaged)
    client = _FakeDocker()
    target = _FakeContainer("owned", labels={"ai-desktop-agent.vm-id": "desk-owned"})
    client.containers.items.append(target)
    pool = VmPool(client=client, state_file=state)
    with pytest.raises(StorageError):
        pool.stop_vm("desk-owned")
    assert not target.stopped and state.read_text() == damaged


@pytest.mark.parametrize("fragment", INVALID)
def test_invalid_backup_extensions_cannot_claim_a_verified_archive(tmp_path, fragment):
    backups = HomeBackups(tmp_path, "my-project")
    name = backups.capture(source_for(archive_bytes()), "desktop")
    archive, _, _ = backups.verified(name)
    contents = archive.read_bytes()
    metadata = archive.with_suffix(".json")
    damaged = extension(metadata.read_text(), fragment)
    metadata.write_text(damaged)
    with pytest.raises(StorageError):
        backups.verified(name)
    assert metadata.read_text() == damaged and archive.read_bytes() == contents


@pytest.mark.parametrize("fragment", INVALID)
def test_unsafe_history_extensions_are_skipped_without_rewriting_the_record(tmp_path, fragment):
    store = TaskStore(tmp_path)
    record = TaskRecord("saved-task", "保存した報告書を確認する", "completed")
    store.save(record)
    path = store._dir / "saved-task.json"
    damaged = extension(path.read_text(), fragment)
    path.write_text(damaged)
    assert store.load(record.id) is None
    assert store.list() == [] and path.read_text() == damaged


@pytest.mark.parametrize("fragment", INVALID)
def test_unsafe_order_extensions_cannot_be_discarded_by_a_new_reservation(tmp_path, fragment):
    store = TaskStore(tmp_path)
    damaged = extension('{"last_order":1}', fragment)
    store._order_file.write_text(damaged)
    with pytest.raises(StorageError):
        store.reserve_session_order()
    assert store._order_file.read_text() == damaged


def test_completing_android_setup_preserves_its_own_valid_extensions_and_other_records(tmp_path):
    state = tmp_path / "state.json"
    extra = [{"label": "報告書 😀", "values": [0, 1.25, False, None, [], {"nested": "valid"}]}]
    records = {
        "and-target": {"done": False, "data_id": "old-data", "future_extension": extra},
        "and-other": {"done": True, "data_id": "known", "annotation": "保存した領域"},
    }
    state.write_text(json.dumps(records))
    setup = AndroidProvisioner(pool=target_pool(), state_file=state)
    setup.mark_done("and-target", data_id="new-data")
    saved = json.loads(state.read_text())
    assert saved["and-target"]["future_extension"] == extra
    assert saved["and-target"]["done"] is True and saved["and-target"]["data_id"] == "new-data"
    assert saved["and-other"] == records["and-other"]


def test_valid_unicode_and_finite_extension_values_remain_readable_across_metadata_readers(
    tmp_path,
):
    extra = [{"label": "報告書 😀", "values": [0, 1.25, False, None, [], {"nested": "valid"}]}]
    state = tmp_path / "vms.json"
    state.write_text(json.dumps({"desk-owned": {"name": "retained", "future_extension": extra}}))
    client = _FakeDocker()
    pool = VmPool(client=client, state_file=state)
    pool.list_vms()
    pool._save_state("desk-owned", {"name": "retained"})
    assert json.loads(state.read_text())["desk-owned"]["future_extension"] == extra
    backups = HomeBackups(tmp_path / "backups", "my-project")
    name = backups.capture(source_for(archive_bytes()), "desktop")
    path = backups.root / (name + ".json")
    data = json.loads(path.read_text()) | {"future_extension": extra}
    path.write_text(json.dumps(data))
    assert backups.verified(name)[1]["future_extension"] == extra
