"""Saved allocation data cannot supply malformed names or authorize mutations."""

import json

import pytest
from httpx import ASGITransport

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.vm_pool import VmPool
from tests.http import AuthenticatedClient
from tests.test_vm_pool import _FakeContainer, _FakeDocker


@pytest.mark.parametrize(
    "record",
    [
        {"name": {"label": "different type"}},
        {"name": "\ud800"},
        {"name": "x" * 129},
        {"vnc_port": True},
        {"ws_port": "5900"},
        {"adb_port": -1},
        {"vnc_port": 65536},
        {"updated_at": False},
        {"updated_at": float("nan")},
        {"updated_at": float("inf")},
        {"updated_at": -1},
        {"updated_at": 10**1000},
        {"isolated_network": ["private-network"]},
        {"isolated_network": ""},
        {"home_volume": None},
        {"home_volume": "\udfff"},
    ],
    ids=[
        "object-name",
        "invalid-name-encoding",
        "oversized-name",
        "boolean-port",
        "string-port",
        "negative-port",
        "oversized-port",
        "boolean-time",
        "nan-time",
        "infinite-time",
        "negative-time",
        "overflow-time",
        "list-network",
        "empty-network",
        "null-home",
        "invalid-home-encoding",
    ],
)
def test_bad_saved_fields_cannot_escape_into_discovery_or_be_overwritten(tmp_path, record):
    path = tmp_path / "vms.json"
    damaged = json.dumps({"desk-owned": record})
    path.write_text(damaged)
    client = _FakeDocker()
    client.containers.items.append(
        _FakeContainer("owned", labels={"ai-desktop-agent.vm-id": "desk-owned"})
    )
    pool = VmPool(client=client, state_file=path)
    with pytest.raises(StorageError):
        pool.list_vms()
    with pytest.raises(StorageError):
        pool._save_state("desk-new", {"name": "new"})
    assert path.read_text() == damaged and client.containers.runs == []


@pytest.mark.parametrize("key", ["..", "../desktop", "", "x" * 129])
def test_invalid_saved_ids_do_not_count_as_an_allocation(tmp_path, key):
    path = tmp_path / "vms.json"
    damaged = json.dumps({key: {"name": "stored"}})
    path.write_text(damaged)
    pool = VmPool(client=_FakeDocker(), state_file=path)
    with pytest.raises(StorageError):
        pool.list_vms()
    assert path.read_text() == damaged


@pytest.mark.parametrize("action", ["prepare_vm", "start_vm", "stop_vm", "restart_vm", "remove_vm"])
@pytest.mark.parametrize("damaged", ["{", '{"desktop":{"name":false}}'])
def test_fixed_environment_rejects_corrupt_storage_before_any_side_effect(
    tmp_path, action, damaged
):
    path = tmp_path / "vms.json"
    path.write_text(damaged)
    client = _FakeDocker()
    target = _FakeContainer(
        "ai-desktop-agent-desktop-1",
        status="created" if action == "start_vm" else "running",
        labels={"com.docker.compose.service": "desktop"},
    )
    client.containers.items.append(target)
    pool = VmPool(client=client, state_file=path)
    networks = set(client.networks.items)
    with pytest.raises(StorageError):
        getattr(pool, action)("desktop")
    assert not any((target.started, target.stopped, target.restarted, target.removed))
    assert set(client.networks.items) == networks
    assert path.read_text() == damaged


def test_partial_legacy_allocations_and_future_fields_remain_readable(tmp_path):
    path = tmp_path / "vms.json"
    original = {
        "desktop": {},
        "desk-old": {"name": "", "vnc_port": 5900, "ws_port": 65535},
        "and-old": {
            "name": "日本語の環境",
            "adb_port": 0,
            "updated_at": 0,
            "isolated_network": "old-project_env_and-old",
            "home_volume": "saved-home",
            "future_extension": {"revision": 2},
        },
    }
    path.write_text(json.dumps(original))
    pool = VmPool(client=_FakeDocker(), state_file=path)
    assert pool._load_all_states() == original
    pool._save_state("desk-new", {"name": "new"})
    current = json.loads(path.read_text())
    assert {key: current[key] for key in original} == original
    assert current["desk-new"]["name"] == "new"


def test_invalid_new_allocation_is_rejected_before_an_atomic_replace(tmp_path):
    path = tmp_path / "vms.json"
    original = '{"desktop":{"name":"preserve"}}'
    path.write_text(original)
    pool = VmPool(client=_FakeDocker(), state_file=path)
    with pytest.raises(StorageError):
        pool._save_state("desk-new", {"name": ["wrong type"]})
    assert path.read_text() == original


async def test_environment_list_reports_storage_failure_instead_of_an_invalid_response(
    tmp_path, monkeypatch
):
    path = tmp_path / "vms.json"
    damaged = '{"desk-owned":{"name":{"unexpected":"object"}}}'
    path.write_text(damaged)
    client = _FakeDocker()
    client.containers.items.append(
        _FakeContainer("owned", labels={"ai-desktop-agent.vm-id": "desk-owned"})
    )
    monkeypatch.setattr(server, "_pool", VmPool(client=client, state_file=path))
    async with AuthenticatedClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as http:
        response = await http.get("/vms")
    assert response.status_code == 503
    assert "VM情報の形式が不正です" in response.json()["detail"]
    assert path.read_text() == damaged
