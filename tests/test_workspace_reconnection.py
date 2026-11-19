"""A failed endpoint restore must not strand the remaining upgrade participants."""

from unittest.mock import Mock

import pytest

from ai_desktop_agent.server.networks import LABEL_ISOLATED, LABEL_PROJECT, LABEL_VM_ID
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.server.workspace_migration import WorkspaceUpgrade
from tests.test_vm_pool import _FakeContainer, _FakeDocker


@pytest.mark.parametrize("fault", ["reload", "connect", "both"])
def test_reconnection_failure_does_not_skip_other_stopped_members(tmp_path, monkeypatch, fault):
    docker = _FakeDocker()
    members = [
        _FakeContainer(
            "desktop", status="exited", labels={"com.docker.compose.service": "desktop"}
        ),
        _FakeContainer(
            "backend-main", status="exited", labels={"com.docker.compose.service": "backend"}
        ),
    ]
    docker.containers.items.extend(members)
    network = docker.networks.create(
        "ai-desktop-agent_env_desktop",
        labels={LABEL_PROJECT: "ai-desktop-agent", LABEL_VM_ID: "desktop", LABEL_ISOLATED: "true"},
    )
    for member in members:
        network.connect(member, aliases=[member.name])
    upgrade = WorkspaceUpgrade(VmPool(client=docker), tmp_path)
    create = docker.networks.create
    attempted = []

    def replacement(*args, **kwargs):
        fresh = create(*args, **kwargs)
        connect = fresh.connect

        def restore(member, **endpoint):
            attempted.append(member.id)
            if member is members[0] and fault in {"connect", "both"}:
                raise RuntimeError("first endpoint unavailable")
            if member is members[1] and fault == "both":
                raise RuntimeError("second endpoint unavailable")
            connect(member, **endpoint)

        monkeypatch.setattr(fresh, "connect", restore)
        if fault == "reload":
            monkeypatch.setattr(
                members[0], "reload", Mock(side_effect=RuntimeError("inspect failed"))
            )
        return fresh

    monkeypatch.setattr(docker.networks, "create", replacement)
    with pytest.raises(DockerUnavailableError, match="再接続") as failure:
        upgrade._reconcile_network("desktop")
    assert members[1].id in attempted
    assert isinstance(failure.value.__cause__, ExceptionGroup)
    assert len(failure.value.__cause__.exceptions) == (2 if fault == "both" else 1)
    if fault != "both":
        endpoint = members[1].attrs["NetworkSettings"]["Networks"][network.name]
        assert endpoint["Aliases"] == [members[1].name]
    assert all(member.status == "exited" and not member.removed for member in members)
