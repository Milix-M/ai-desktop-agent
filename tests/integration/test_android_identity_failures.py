"""An actual unreadable or corrupt disk cannot acquire a new setup identity."""

import io
import json
import os
import shutil
import subprocess
import tarfile
import uuid
from contextlib import suppress

import docker
import pytest
from docker.errors import NotFound

from ai_desktop_agent.server.networks import LABEL_PROJECT

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real Docker opt-in"),
]

PROBE = r"""
import json,os,sys
from pathlib import Path
from types import SimpleNamespace
from ai_desktop_agent.server.android_provision import AndroidProvisioner
mode=sys.argv[1]
state=Path('/input/state.json')
before=state.read_bytes()
calls=[]
def identity(vm_id):
    calls.append(vm_id)
    return 'same-physical-container'
setup=AndroidProvisioner(pool=SimpleNamespace(instance_id=identity),state_file=state)
proof={'mode':mode,'uid':os.getuid(),'calls':calls,'errors':{}}
for name in ['needs','mark_done']:
    try:
        proof[name]=getattr(setup,name)('android')
    except Exception as error:
        proof['errors'][name]={'type':type(error).__name__,
            'cause':type(error.__cause__).__name__ if error.__cause__ else None}
proof['state_unchanged']=state.read_bytes()==before
print(json.dumps(proof))
"""


@pytest.mark.parametrize(
    "damage", ["unreadable", "invalid-header", "truncated", "zero-uuid", "valid"]
)
def test_real_unprivileged_disk_inspection_preserves_the_provisioning_record(tmp_path, damage):
    disk = tmp_path / "android-data.img"
    volume_uuid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    if damage == "valid":
        mkfs = shutil.which("mkfs.ext4")
        if not mkfs:
            pytest.skip("mkfs.ext4 is needed for the real filesystem identity check")
        with disk.open("wb") as file:
            file.truncate(16 * 1024 * 1024)
        subprocess.run(
            [mkfs, "-q", "-F", "-U", str(volume_uuid), str(disk)],
            check=True,
            capture_output=True,
            timeout=10,
        )
    else:
        block = bytearray(1024 + 0x78)
        block[1024 + 0x38 : 1024 + 0x3A] = b"\x53\xef"
        block[1024 + 0x68 : 1024 + 0x78] = volume_uuid.bytes
        if damage == "invalid-header":
            block[1024 + 0x38 : 1024 + 0x3A] = b"\0\0"
        elif damage == "zero-uuid":
            block[1024 + 0x68 : 1024 + 0x78] = b"\0" * 16
        disk.write_bytes(b"truncated data" if damage == "truncated" else block)
    original = json.dumps(
        {"android": {"done": True, "data_id": "ext4:" + str(volume_uuid), "at": 1}}
    )
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for directory in ["input", "app/android-host"]:
            entry = tarfile.TarInfo(directory)
            entry.type, entry.mode = tarfile.DIRTYPE, 0o755
            tar.addfile(entry)
        for name, data, mode in [
            ("input/state.json", original.encode(), 0o644),
            (
                "app/android-host/android-data.img",
                disk.read_bytes(),
                0 if damage == "unreadable" else 0o644,
            ),
        ]:
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(data), mode
            tar.addfile(entry, io.BytesIO(data))
    client = docker.from_env(timeout=30)
    backend = None
    project = "android-data-" + uuid.uuid4().hex[:10]
    try:
        backend = client.containers.run(
            os.environ.get("BACKEND_SMOKE_IMAGE", "ai-desktop-agent-backend-smoke"),
            name=project,
            entrypoint=["sleep"],
            command=["120"],
            detach=True,
            network_mode="none",
            cap_drop=["ALL"],
            labels={LABEL_PROJECT: project},
        )
        assert backend.put_archive("/", archive.getvalue())
        result = backend.exec_run(
            ["/app/.venv/bin/python", "-c", PROBE, damage], user="65534:65534"
        )
        assert result.exit_code == 0, result.output.decode(errors="replace")
        proof = json.loads(result.output)
        (tmp_path / "android-identity-proof.json").write_text(json.dumps(proof, indent=2))
        assert proof["uid"] == 65534 and proof["calls"] == [], proof
        assert proof["state_unchanged"], proof
        if damage == "valid":
            assert proof["needs"] is False, proof
            # The disk is readable, but this user cannot replace the root-owned record.
            assert proof["errors"] == {
                "mark_done": {"type": "StorageError", "cause": "PermissionError"}
            }, proof
        else:
            cause = "PermissionError" if damage == "unreadable" else None
            assert proof["errors"] == {
                name: {"type": "StorageError", "cause": cause} for name in ["needs", "mark_done"]
            }, proof
    finally:
        if backend is not None:
            with suppress(NotFound):
                backend.remove(force=True)
        client.close()
