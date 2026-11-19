"""A process interrupted during receipt publication recovers without a replay."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ai_desktop_agent.server.vm_creations import CreationStore, VmCreationService
from ai_desktop_agent.server.vm_pool import VmPool
from tests.creation_shutdown_probe import KEY, NAME
from tests.test_vm_pool import _FakeDocker

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("phase", ["before-write", "after-write"])
async def test_event_loop_teardown_drains_publication_and_recovers_exactly_once(
    tmp_path, monkeypatch, phase
):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "ai-desktop-agent")
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "tests.creation_shutdown_probe", str(tmp_path), phase],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    proof = json.loads(result.stdout.splitlines()[-1])
    assert proof["owner_retained_during_write"]
    assert proof["record_state"] == "queued" and proof["launches"] == 0
    assert proof["capacity_released"] == 8
    assert proof["receipt_existed_at_cancellation"] == (phase == "after-write")
    store = CreationStore(tmp_path / "requests")
    store.acquire_owner().close()
    client = _FakeDocker()
    service = VmCreationService(VmPool(client=client, state_file=tmp_path / "vms.json"), store)
    try:
        await service.start()
        async with asyncio.timeout(5):
            recovered = await service.wait(proof["job_id"])
        assert recovered.state == "succeeded" and len(client.containers.runs) == 1
        retried = await service.submit(KEY, NAME, "container")
        assert retried.id == recovered.id and retried.vm_id == recovered.vm_id
        assert len(client.containers.runs) == 1
        proof.update(
            recovered_state=recovered.state, recovered_vm_id=recovered.vm_id, final_launches=1
        )
        (tmp_path / "forced-shutdown-proof.json").write_text(json.dumps(proof, indent=2))
    finally:
        await service.close()
