"""Untrusted setup metadata must not trigger setup or replace the saved evidence."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from ai_desktop_agent.server.android_provision import AndroidProvisioner
from ai_desktop_agent.server.persistence import StorageError


@pytest.mark.parametrize(
    "key,record",
    [
        ("and-bad", {"done": True, "data_id": ""}),
        ("and-bad", {"done": True, "data_id": " \t"}),
        ("and-bad", {"done": True, "data_id": "\ud800"}),
        ("and-bad", {"done": True, "data_id": "known", "at": None}),
        ("and-bad", {"done": True, "data_id": "known", "at": True}),
        ("and-bad", {"done": True, "data_id": "known", "at": "yesterday"}),
        ("and-bad", {"done": True, "data_id": "known", "at": -1}),
        ("and-bad", {"done": True, "data_id": "known", "at": float("nan")}),
        ("and-bad", {"done": True, "data_id": "known", "at": float("inf")}),
        ("and-bad", {"done": True, "data_id": "known", "at": 10**1000}),
        ("", {"done": True, "data_id": "known"}),
        ("../android", {"done": True, "data_id": "known"}),
        ("x" * 129, {"done": True, "data_id": "known"}),
        ("\ud800", {"done": True, "data_id": "known"}),
    ],
    ids=[
        "empty-data",
        "blank-data",
        "invalid-data-encoding",
        "null-time",
        "boolean-time",
        "string-time",
        "negative-time",
        "nan-time",
        "infinite-time",
        "overflow-time",
        "empty-id",
        "path-id",
        "long-id",
        "invalid-id-encoding",
    ],
)
def test_invalid_records_prevent_reads_and_new_writes_without_container_discovery(
    tmp_path, key, record
):
    state = tmp_path / "state.json"
    original = json.dumps({"and-kept": {"done": True, "data_id": "container:kept"}, key: record})
    state.write_text(original)
    pool = Mock()
    pool.instance_id.return_value = "kept"
    setup = AndroidProvisioner(pool=pool, state_file=state)
    with pytest.raises(StorageError):
        setup.needs("and-kept")
    with pytest.raises(StorageError):
        setup.mark_done("and-new", data_id="new-data")
    assert state.read_text() == original
    pool.instance_id.assert_not_called()


@pytest.mark.parametrize("data_id", [True, 12, [], {}, "", " \t", "\ud800"])
def test_invalid_explicit_identity_cannot_poison_a_readable_record(tmp_path, data_id):
    state = tmp_path / "state.json"
    original = '{"and-kept":{"done":true,"data_id":"container:kept"}}'
    state.write_text(original)
    pool = Mock()
    pool.instance_id.return_value = "kept"
    setup = AndroidProvisioner(pool=pool, state_file=state)
    with pytest.raises(ValueError):
        setup.mark_done("and-kept", data_id=data_id)
    assert state.read_text() == original
    assert not state.with_suffix(".json.lock").exists()
    pool.instance_id.assert_not_called()
    assert setup.needs("and-kept") is False


@pytest.mark.parametrize("vm_id", [None, True, "", "../android", "x" * 129, "\ud800"])
def test_public_invalid_environment_id_cannot_create_setup_metadata(tmp_path, vm_id):
    state = tmp_path / "state.json"
    setup = AndroidProvisioner(pool=Mock(), state_file=state)
    with pytest.raises(ValueError):
        setup.needs(vm_id)
    with pytest.raises(ValueError):
        setup.mark_done(vm_id, data_id="known")
    assert list(tmp_path.iterdir()) == []


def test_legacy_records_and_future_fields_survive_an_unrelated_completion(tmp_path):
    state = tmp_path / "state.json"
    records = {
        "android": {"done": True, "data_id": "d1"},
        "and-old": {
            "done": False,
            "data_id": "known",
            "at": 0,
            "future_extension": {"revision": 2},
        },
        "and-existing": {"done": True, "data_id": "container:kept", "at": 1.5},
    }
    state.write_text(json.dumps(records))
    setup = AndroidProvisioner(pool=Mock(), state_file=state)
    setup.mark_done("and-new", data_id="new-data")
    current = json.loads(state.read_text())
    assert {key: current[key] for key in records} == records
    assert current["and-new"]["data_id"] == "new-data" and current["and-new"]["done"] is True


def test_corrupt_state_is_reported_before_attempting_to_resolve_identity(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{broken")
    pool = Mock()
    pool.instance_id.side_effect = RuntimeError("identity must not be consulted")
    setup = AndroidProvisioner(pool=pool, state_file=state)
    with pytest.raises(StorageError):
        setup.mark_done("and-new")
    pool.instance_id.assert_not_called()
    assert state.read_text() == "{broken"


def test_an_absent_container_cannot_be_recorded_as_configured(tmp_path):
    pool = Mock()
    pool.instance_id.return_value = None
    state = tmp_path / "state.json"
    setup = AndroidProvisioner(pool=pool, state_file=state)
    with pytest.raises(StorageError):
        setup.mark_done("and-absent")
    assert not state.exists()


@pytest.mark.parametrize("change", ["completion", "corruption"])
def test_identity_lookup_cannot_overwrite_metadata_changed_by_another_writer(tmp_path, change):
    entered, release = threading.Event(), threading.Event()
    state = tmp_path / "state.json"
    state.write_text("{}")
    pool = Mock()

    def identity(vm_id):
        entered.set()
        assert release.wait(2)
        return "first-container"

    pool.instance_id.side_effect = identity
    setup = AndroidProvisioner(pool=pool, state_file=state)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(setup.mark_done, "and-first")
        try:
            assert entered.wait(1)
            if change == "completion":
                setup.mark_done("and-second", data_id="second-data")
            else:
                state.write_text('{"and-damaged":{"done":true,"data_id":""}}')
            concurrent = state.read_text()
        finally:
            release.set()
        if change == "corruption":
            with pytest.raises(StorageError):
                result.result(timeout=1)
            assert state.read_text() == concurrent
        else:
            result.result(timeout=1)
            saved = json.loads(state.read_text())
            assert saved["and-first"]["data_id"] == "container:first-container"
            assert saved["and-second"]["data_id"] == "second-data"
