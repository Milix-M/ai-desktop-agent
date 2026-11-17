"""Keep desktop guests within finite, configurable Docker resource limits."""

from unittest.mock import Mock

import pytest

from ai_desktop_agent.server.vm_pool import VmPool
from tests.test_vm_pool import _FakeDocker


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("DESKTOP_MEMORY_MB", "DESKTOP_CPUS", "DESKTOP_PIDS_LIMIT"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("custom", [False, True])
def test_new_desktops_have_bounded_resources(tmp_path, monkeypatch, custom):
    if custom:
        monkeypatch.setenv("DESKTOP_MEMORY_MB", "3072")
        monkeypatch.setenv("DESKTOP_CPUS", "1.5")
        monkeypatch.setenv("DESKTOP_PIDS_LIMIT", "768")
    client = _FakeDocker()
    pool = VmPool(client=client, state_file=tmp_path / "vms.json")
    pool.create_vm(kind="container")
    run = client.containers.runs[-1]
    assert run.get("mem_limit") == (3072 if custom else 2048) * 1024**2
    assert run.get("memswap_limit") == run["mem_limit"]
    assert run.get("nano_cpus") == (1_500_000_000 if custom else 2_000_000_000)
    assert run.get("pids_limit") == (768 if custom else 1024)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DESKTOP_MEMORY_MB", "0"),
        ("DESKTOP_MEMORY_MB", "5"),
        ("DESKTOP_MEMORY_MB", "1024.5"),
        ("DESKTOP_MEMORY_MB", "999999999999999999"),
        ("DESKTOP_CPUS", "NaN"),
        ("DESKTOP_CPUS", "Infinity"),
        ("DESKTOP_CPUS", "0"),
        ("DESKTOP_CPUS", "0.001"),
        ("DESKTOP_CPUS", "1.0000000001"),
        ("DESKTOP_CPUS", "1e999999999999999999999999"),
        ("DESKTOP_PIDS_LIMIT", "0"),
        ("DESKTOP_PIDS_LIMIT", "-1"),
        ("DESKTOP_PIDS_LIMIT", "1.5"),
        ("DESKTOP_PIDS_LIMIT", "1_024"),
    ],
)
async def test_invalid_limits_fail_before_allocating_or_recovering(
    tmp_path, monkeypatch, name, value
):
    from ai_desktop_agent.server import app

    monkeypatch.setenv(name, value)
    client = _FakeDocker()
    pool = VmPool(client=client, state_file=tmp_path / "vms.json")
    with pytest.raises(ValueError, match=name):
        pool.create_vm(kind="container")
    assert client.containers.runs == []
    assert not (tmp_path / "vms.json").exists()
    assert len(client.networks.items) == 1  # Only the existing management network.

    creations = Mock()
    monkeypatch.setattr(app, "get_vm_creations", creations)
    with pytest.raises(ValueError, match=name):
        async with app.lifespan(app.app):
            pytest.fail("invalid resource limits were accepted")
    creations.assert_not_called()
