from unittest.mock import Mock

import pytest

from ai_desktop_agent.server.vm_pool import LABEL_PROJECT, LABEL_VM_ID, LABEL_WS_FOR, VmPool
from tests.test_vm_pool import _FakeContainer, _FakeDocker


def test_retirement_removes_only_this_projects_legacy_relays_and_keeps_work(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "mine")
    docker = _FakeDocker()
    pool = VmPool(client=docker, state_file=tmp_path / "state.json")
    main = _FakeContainer("mine-desktop", labels={LABEL_PROJECT: "mine", LABEL_VM_ID: "desk-kept"})
    fixed = _FakeContainer(
        "mine-relay",
        labels={
            "com.docker.compose.project": "mine",
            "com.docker.compose.service": "websockify-desktop",
        },
    )
    dynamic = _FakeContainer(
        "dynamic-relay",
        labels={LABEL_PROJECT: "mine", LABEL_VM_ID: "desk-kept", LABEL_WS_FOR: "desk-kept"},
    )
    outsider = _FakeContainer(
        "other-relay",
        labels={
            LABEL_PROJECT: "other",
            "com.docker.compose.project": "other",
            "com.docker.compose.service": "websockify",
            LABEL_VM_ID: "desk-other",
            LABEL_WS_FOR: "desk-other",
        },
    )
    docker.containers.items.extend([main, fixed, dynamic, outsider])
    work = tmp_path / "saved-work"
    work.write_text("saved document")
    pool.retire_legacy_relays()
    assert fixed.removed and dynamic.removed
    assert not main.removed and not outsider.removed
    assert work.read_text() == "saved document"
    pool.retire_legacy_relays()


def test_relay_retirement_failure_cannot_silently_leave_an_unauthenticated_route(tmp_path):
    docker = _FakeDocker()
    relay = _FakeContainer("relay", labels={"com.docker.compose.service": "websockify"})
    relay.remove = Mock(side_effect=RuntimeError("Docker failure"))
    docker.containers.items.append(relay)
    with pytest.raises(RuntimeError, match="Docker failure"):
        VmPool(client=docker, state_file=tmp_path / "state.json").retire_legacy_relays()
