import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_desktop_agent.server import app as server
from ai_desktop_agent.server.vm_control import VmStatusInfo


@pytest.mark.parametrize("latest_is_target", [False, True])
async def test_restart_stops_only_fixed_vm_and_waits_for_cleanup(monkeypatch, latest_is_target):
    cleanup = asyncio.Event()

    async def work():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleanup.set()

    task = asyncio.create_task(work())
    await asyncio.sleep(0)
    target = SimpleNamespace(id="target", vm_id="vm", is_running=True, _task=task, stop=task.cancel)
    other = SimpleNamespace(id="other", vm_id="desk-other", is_running=True, stop=Mock())
    monkeypatch.setattr(server, "_sessions", {"target": target, "other": other})
    latest = target if latest_is_target else other
    monkeypatch.setattr(server, "_active_session", latest)

    def restart(*, expected_vm_id):
        assert expected_vm_id == "vm"
        assert cleanup.is_set()
        return VmStatusInfo(running=True, status="running")

    monkeypatch.setattr(
        server,
        "_get_vm_controller",
        lambda: SimpleNamespace(target_vm_id=lambda: "vm", restart=restart),
    )
    await server.vm_restart()
    other.stop.assert_not_called()
    assert server._active_session is (None if latest_is_target else latest)
    assert task.cancelled()
