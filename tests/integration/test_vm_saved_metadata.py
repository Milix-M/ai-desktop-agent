"""Corrupt fixed-environment metadata never dispatches real lifecycle commands."""

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
@pytest.mark.parametrize("damage", ["syntax", "name-type", "extension-nan", "extension-unicode"])
def test_corrupt_fixed_metadata_cannot_mutate_an_actual_container(
    tmp_path, monkeypatch, action, damage
):
    client = docker.from_env(timeout=15)
    project = "saved-vm-data-" + uuid.uuid4().hex[:10]
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", project)
    image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    created, calls = [], []
    path = tmp_path / "state.json"
    if damage in {"syntax", "name-type"}:
        damaged = "{" if damage == "syntax" else '{"desktop":{"name":false}}'
    else:
        value = "NaN" if damage == "extension-nan" else r'"\ud800"'
        damaged = '{"desktop":{"name":"kept","future_extension":[{"value":' + value + "}]}}"
    path.write_text(damaged)
    pool = VmPool(client=client, state_file=path)
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
                labels={
                    LABEL_PROJECT: owner,
                    LABEL_VM_ID: "desktop",
                    LABEL_KIND: "container",
                    "com.docker.compose.project": owner,
                    "com.docker.compose.service": "desktop",
                },
            )
            created.append(target)
            if action != "start_vm":
                target.start()
            target.reload()
        before = {
            c.id: {"status": c.status, "started": c.attrs["State"]["StartedAt"]} for c in created
        }
        main = created[0]
        proof = {"action": action, "damage": damage, "before": before, "damaged": damaged}
        with ExitStack() as stack:
            for method in ["start", "stop", "restart", "remove"]:
                original = getattr(type(main), method)

                def record(target, *args, _method=method, _original=original, **kwargs):
                    calls.append({"method": _method, "id": target.id})
                    return _original(target, *args, **kwargs)

                stack.enter_context(patch.object(type(main), method, record))
            try:
                options = {"timeout": 1} if action in {"stop_vm", "restart_vm"} else {}
                getattr(pool, action)("desktop", **options)
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
        proof.update(calls=calls, after=after, saved=path.read_text())
        (tmp_path / "saved-metadata-proof.json").write_text(json.dumps(proof, indent=2))
        assert proof.get("error_type") == "StorageError", proof
        assert calls == [] and after == before and proof["saved"] == damaged, proof
    finally:
        for target in created:
            with suppress(NotFound):
                target.remove(force=True)
        pool.close()
        client.close()
