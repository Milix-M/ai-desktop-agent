from pathlib import Path

import pytest
import yaml

from ai_desktop_agent.server.vm_pool import VmPool
from tests.test_vm_pool import _FakeDocker


def test_container_only_build_provides_dynamic_relay_image(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBSOCKIFY_IMAGE", raising=False)
    config = yaml.safe_load((Path(__file__).parents[1] / "docker-compose.yml").read_text())
    docker = _FakeDocker()
    pool = VmPool(client=docker, state_file=tmp_path / "state.json")
    pool.create_vm(kind="container")
    relay_image = docker.containers.runs[-1]["image"]
    assert config["services"]["websockify-desktop"]["image"] == (
        "${WEBSOCKIFY_IMAGE:-" + relay_image + "}"
    )
    for service in ("websockify", "websockify-android"):
        assert (
            config["services"][service]["image"]
            == config["services"]["websockify-desktop"]["image"]
        )


def test_relay_failure_rolls_back_created_desktop(tmp_path, monkeypatch):
    docker = _FakeDocker()
    pool = VmPool(client=docker, state_file=tmp_path / "state.json")
    original = docker.containers.run

    def run(image, **kwargs):
        if "ws-" in kwargs.get("name", ""):
            raise RuntimeError("relay failed")
        return original(image, **kwargs)

    monkeypatch.setattr(docker.containers, "run", run)
    with pytest.raises(RuntimeError, match="relay failed"):
        pool.create_vm(kind="container")
    assert pool.list_vms() == []
    assert docker.containers.items[0].removed
