"""Child process for a pending creation during actual asyncio.Runner teardown."""

import asyncio
import json
import sys
import threading
from pathlib import Path

from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_creations import CreationStore, VmCreationService
from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.vm.async_io import reserve_vm_creation_slot
from tests.test_vm_pool import _FakeDocker

KEY = "loop-shutdown-owned-key-0123456789"
NAME = "saved environment"


def main(directory, phase):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    entered, cancelled, release = threading.Event(), threading.Event(), threading.Event()
    proof = {}
    client = _FakeDocker()

    class Store(CreationStore):
        def write(self, record):
            if record.state == "queued":
                if phase == "after-write":
                    super().write(record)
                proof["job_id"] = record.id
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("controller did not release the durable write")
                if phase == "before-write":
                    super().write(record)
            else:
                super().write(record)

    store = Store(root / "requests")
    service = VmCreationService(VmPool(client=client, state_file=root / "vms.json"), store)

    def control():
        try:
            if not cancelled.wait(10):
                raise RuntimeError("event-loop teardown did not cancel the pending request")
            proof["receipt_existed_at_cancellation"] = store._path(proof["job_id"]).exists()
            try:
                other = CreationStore(store.directory).acquire_owner()
            except DockerUnavailableError:
                proof["owner_retained_during_write"] = True
            else:
                other.close()
                raise RuntimeError("ownership was released during an unfinished write")
        except Exception as error:
            proof["controller_error"] = str(error)
        finally:
            release.set()

    controller = threading.Thread(target=control)
    controller.start()

    async def request():
        try:
            await service.submit(KEY, NAME, "container")
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def run_until_forced_teardown():
        asyncio.create_task(request())
        if not await asyncio.to_thread(entered.wait, 5):
            raise RuntimeError("acceptance did not reach the durable-write barrier")
        # The process is stopping with unfinished work: Runner cancels all remaining
        # tasks. Do not call or cancel the coordinator's private tasks in the test.

    asyncio.run(run_until_forced_teardown())
    controller.join(10)
    assert not controller.is_alive()
    proof["record_state"] = store.read(proof["job_id"]).state
    proof["launches"] = len(client.containers.runs)
    slots = [reserve_vm_creation_slot() for _ in range(8)]
    for slot in slots:
        slot.release()
    proof["capacity_released"] = len(slots)
    proof["phase"] = phase
    print(json.dumps(proof))
    assert "controller_error" not in proof
    assert proof["owner_retained_during_write"]
    assert proof["receipt_existed_at_cancellation"] == (phase == "after-write")
    assert proof["record_state"] == "queued" and proof["launches"] == 0
    # The kernel releases the owner descriptor when this child process exits.


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
