"""Lifecycle decisions and commands refer to the same actual Docker container."""

import json
import os
import uuid
from contextlib import suppress
from unittest.mock import patch

import docker
import pytest
from docker.errors import NotFound
from requests.exceptions import ReadTimeout

from ai_desktop_agent.server.vm_pool import (
    LABEL_DISK_COPY,
    LABEL_KIND,
    LABEL_PROJECT,
    LABEL_VM_ID,
    VmPool,
)

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real Docker opt-in"),
]


@pytest.mark.parametrize(
    "mode,operation",
    [
        ("lookup-failure", "start_vm"),
        ("lookup-failure", "restart_vm"),
        ("replacement-kind", "start_vm"),
        ("replacement-kind", "restart_vm"),
        ("ambiguous", "start_vm"),
        ("ambiguous", "restart_vm"),
        ("copy-replay", "start_vm"),
    ],
)
def test_selected_container_controls_kvm_checks_and_receives_only_one_command(
    tmp_path, monkeypatch, mode, operation
):
    client = docker.from_env(timeout=15)
    project = "lifecycle-selection-" + uuid.uuid4().hex[:10]
    vm_id = "desk-selected"
    labels = {LABEL_PROJECT: project, LABEL_VM_ID: vm_id}
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", project)
    monkeypatch.setenv("USE_KVM", "false")
    monkeypatch.setenv("ALLOW_TCG_VM", "false")
    image = os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke")
    created = []
    commands = []
    pool = VmPool(client=client, state_file=tmp_path / "state.json")

    def container(name, kind="container", *, extra=None, command=None, volumes=None):
        result = client.containers.create(
            image,
            name=project + "-" + name,
            entrypoint=["/app/.venv/bin/python", "-c"],
            command=[command or "import time;time.sleep(180)"],
            labels={**labels, LABEL_KIND: kind, **(extra or {})},
            volumes=volumes,
            network_mode="none",
            cap_drop=["ALL"],
            user=f"{os.getuid()}:{os.getgid()}",
        )
        created.append(result)
        return result

    proof = {"mode": mode, "operation": operation}
    try:
        primary = container("primary", "qemu" if mode == "replacement-kind" else "container")
        replacement = None
        if mode in {"replacement-kind", "ambiguous"}:
            replacement = container("replacement")
        copied = tmp_path / "copy-count"
        if mode == "copy-replay":
            preparer = container(
                "preparer",
                extra={LABEL_DISK_COPY: vm_id},
                volumes={str(tmp_path): {"bind": "/proof", "mode": "rw"}},
                command="from pathlib import Path;p=Path('/proof/copy-count');"
                "p.write_text((p.read_text() if p.exists() else '')+'copy\\n')",
            )
            preparer.start()
            assert preparer.wait(timeout=10)["StatusCode"] == 0, preparer.logs()
            assert copied.read_text() == "copy\n"
        if operation == "restart_vm":
            primary.start()
        for item in created:
            item.reload()
        proof["before"] = {c.id: c.attrs["State"]["StartedAt"] for c in created}
        listing = type(client.containers).list
        command_method = "start" if operation == "start_vm" else "restart"
        invoke = getattr(type(primary), command_method)
        reads = 0

        def discover(collection, *args, **kwargs):
            nonlocal reads
            filters = (kwargs.get("filters") or {}).get("label", [])
            if f"{LABEL_VM_ID}={vm_id}" in filters:
                reads += 1
                if mode == "lookup-failure" and reads > 1 and not commands:
                    raise ReadTimeout("injected discovery failure before the command")
                if mode == "replacement-kind":
                    return [primary] if reads == 1 else [replacement]
            return listing(collection, *args, **kwargs)

        def record(target, *args, **kwargs):
            commands.append(target.id)
            return invoke(target, *args, **kwargs)

        with (
            patch.object(type(client.containers), "list", discover),
            patch.object(type(primary), command_method, record),
        ):
            try:
                options = {"timeout": 1} if operation == "restart_vm" else {}
                result = getattr(pool, operation)(vm_id, **options)
                proof["result"] = result.to_dict()
            except Exception as error:
                proof.update(error_type=type(error).__name__, error=str(error))
        for item in created:
            item.reload()
        proof["after"] = {c.id: c.attrs["State"]["StartedAt"] for c in created}
        proof["commands"] = commands
        proof["primary"] = primary.id
        if mode == "copy-replay":
            proof["copy_count"] = copied.read_text()
        (tmp_path / "lifecycle-selection-proof.json").write_text(json.dumps(proof, indent=2))
        if mode in {"replacement-kind", "ambiguous"}:
            assert proof.get("error_type") == (
                "KvmUnavailableError" if mode == "replacement-kind" else "DockerUnavailableError"
            ), proof
            assert commands == [] and proof["after"] == proof["before"]
        else:
            assert "error_type" not in proof, proof
            assert commands == [primary.id]
            assert primary.status == "running"
            assert proof["after"][primary.id] != proof["before"][primary.id]
            if mode == "copy-replay":
                assert proof["copy_count"] == "copy\n"
    finally:
        for item in created:
            with suppress(NotFound):
                item.remove(force=True)
        pool.close()
        client.close()
