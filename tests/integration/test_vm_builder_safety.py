"""Exercise cleanup against a real busy mount in a disposable mount namespace."""

import io
import os
import tarfile
import uuid
from pathlib import Path

import docker
import pytest

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real mount opt-in"),
]

PROBE = b"""#!/bin/bash
set -euo pipefail
source /tmp/builder.sh
WORK_DIR=$(mktemp -d /tmp/vm-builder-smoke.XXXXXXXX)
TARGET="$WORK_DIR/rootfs"
mkdir -p "$TARGET/dev" "$TARGET/sys" "$TARGET/proc"
echo preserve > "$TARGET/sentinel"
mount -t tmpfs -o size=1m tmpfs "$TARGET/dev"
bash -c 'cd "$1"; touch ready; exec sleep 30' bash "$TARGET/dev" &
holder=$!
for _ in {1..200}; do
    [ -f "$TARGET/dev/ready" ] && break
    sleep 0.01
done
test -f "$TARGET/dev/ready"
if (cleanup); then
    echo "busy mount was not preserved" >&2
    exit 1
fi
test -f "$TARGET/sentinel"
test -f "$TARGET/dev/ready"
echo BUSY_MOUNT_PRESERVED
kill "$holder"
wait "$holder" || true
umount "$TARGET/dev"
(cleanup)
test ! -e "$WORK_DIR"
echo UNMOUNTED_WORKTREE_REMOVED
"""


def test_busy_mount_is_preserved_and_a_later_cleanup_can_remove_the_worktree():
    image = os.environ.get("QMP_SMOKE_IMAGE", "ai-desktop-agent-qmp-smoke")
    script = Path(__file__).resolve().parents[2] / "vm/build-vm-image.sh"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, body in (("builder.sh", script.read_bytes()), ("probe.sh", PROBE)):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(body), 0o500
            tar.addfile(info, io.BytesIO(body))
    client = docker.from_env(timeout=15)
    container = None
    try:
        container = client.containers.create(
            image,
            entrypoint="bash",
            command=["/tmp/probe.sh"],
            network_mode="none",
            cap_drop=["ALL"],
            cap_add=["SYS_ADMIN"],
            security_opt=["apparmor=unconfined"],
            mem_limit="128m",
            nano_cpus=500_000_000,
            pids_limit=32,
            labels={"ai-desktop-agent.smoke": "vm-builder-" + uuid.uuid4().hex},
        )
        container.put_archive("/tmp", archive.getvalue())
        container.start()
        result = container.wait(timeout=10)
        logs = container.logs().decode(errors="replace")
        assert result["StatusCode"] == 0, logs
        assert "BUSY_MOUNT_PRESERVED" in logs
        assert "UNMOUNTED_WORKTREE_REMOVED" in logs
    finally:
        if container is not None:
            container.remove(force=True)
        client.close()
