"""Invalid logical IDs never dispatch lifecycle commands to real containers."""

import json
import os
import uuid
from contextlib import ExitStack, suppress
from unittest.mock import patch

import docker
import pytest
from docker.errors import NotFound

from ai_desktop_agent.server.vm_pool import LABEL_KIND, LABEL_PROJECT, LABEL_VM_ID, VmPool

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real Docker opt-in"),
]


@pytest.mark.parametrize("action", ["start_vm", "stop_vm", "restart_vm", "remove_vm"])
def test_invalid_ids_cannot_operate_on_an_actual_owned_container(tmp_path, monkeypatch, action):
    client = docker.from_env(timeout=15)
    project = "invalid-vm-id-" + uuid.uuid4().hex[:10]
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", project)
    image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    created, calls = [], []
    pool = VmPool(client=client, state_file=tmp_path / "state.json")
    try:
        for suffix, owner in [("primary", project), ("other", project + "-other")]:
            target = client.containers.create(
                image,
                name=project + "-" + suffix,
                network_mode="none",
                cap_drop=["ALL"],
                entrypoint=["python3", "-c"],
                command=[
                    "import signal,sys,time;"
                    "signal.signal(signal.SIGTERM,lambda *_:sys.exit(0));time.sleep(180)"
                ],
                labels={LABEL_PROJECT: owner, LABEL_VM_ID: "..", LABEL_KIND: "container"},
            )
            created.append(target)
            if action != "start_vm":
                target.start()
            target.reload()
        before = {
            c.id: {"status": c.status, "started": c.attrs["State"]["StartedAt"]} for c in created
        }
        main = created[0]
        proof = {"action": action, "before": before}
        with ExitStack() as stack:
            for method in ["start", "stop", "restart", "remove"]:
                original = getattr(type(main), method)

                def record(target, *args, _method=method, _original=original, **kwargs):
                    calls.append({"method": _method, "id": target.id})
                    return _original(target, *args, **kwargs)

                stack.enter_context(patch.object(type(main), method, record))
            try:
                options = {"timeout": 1} if action in {"stop_vm", "restart_vm"} else {}
                getattr(pool, action)("..", **options)
            except Exception as error:
                proof.update(error_type=type(error).__name__, error=str(error))
        after = {}
        for target in created:
            try:
                target.reload()
                after[target.id] = {
                    "status": target.status,
                    "started": target.attrs["State"]["StartedAt"],
                }
            except NotFound:
                after[target.id] = {"removed": True}
        proof.update(calls=calls, after=after)
        (tmp_path / "invalid-id-proof.json").write_text(json.dumps(proof, indent=2))
        assert proof.get("error_type") == "ValueError", proof
        assert calls == [] and after == before, proof
    finally:
        for target in created:
            with suppress(NotFound):
                target.remove(force=True)
        pool.close()
        client.close()
