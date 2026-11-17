"""Read real qcow2/ext4 boot files and exercise image locks; no guest OS boot."""

import io
import os
import tarfile
import uuid

import docker
import pytest

pytestmark = [
    pytest.mark.desktop_integration,
    pytest.mark.skipif(os.environ.get("RUN_DESKTOP_SMOKE") != "1", reason="real disk opt-in"),
]

PROBE = b"""#!/bin/bash
set -euo pipefail
work=$(mktemp -d /tmp/guest-boot.XXXXXXXX)
mkdir -p "$work/source/boot" "$work/source/lib/modules/6.8.0-old" \
    "$work/source/lib/modules/6.10.0-incomplete" "$work/source/etc/ai-desktop-agent" \
    "$work/source/home/agent" "$work/bin" "$work/instances"
echo kernel-for-saved-work > "$work/source/boot/vmlinuz-6.8.0-old"
echo initrd-for-saved-work > "$work/source/boot/initrd.img-6.8.0-old"
echo incomplete > "$work/source/boot/vmlinuz-6.10.0-incomplete"
echo 'console=ttyS0 root=/dev/vda rw original=1' > "$work/source/etc/ai-desktop-agent/cmdline"
echo 'saved document' > "$work/source/home/agent/work.txt"
truncate -s 64M "$work/base.raw"
mkfs.ext4 -q -F -d "$work/source" "$work/base.raw"
qemu-img convert -f raw -O qcow2 "$work/base.raw" "$work/base.qcow2"
cat > "$work/bin/qemu-system-x86_64" <<'STUB'
#!/bin/bash
set -euo pipefail
while [ "$#" -gt 0 ]; do
    case "$1" in
        -kernel) cat "$2" > "$BOOT_RECORD.kernel"; shift ;;
        -initrd) cat "$2" > "$BOOT_RECORD.initrd"; shift ;;
        -append) printf '%s' "$2" > "$BOOT_RECORD.cmdline"; shift ;;
    esac
    shift
done
STUB
chmod +x "$work/bin/qemu-system-x86_64"
export PATH="$work/bin:$PATH" USE_KVM=false ALLOW_TCG_VM=true
export VM_BASE_IMAGE="$work/base.qcow2" VM_IMAGE="$work/instances/saved.qcow2"
export QMP_SOCK="$work/qmp.sock" QGA_SOCK="$work/qga.sock" BOOT_RECORD="$work/record"
bash /entrypoint.sh
grep -qx kernel-for-saved-work "$BOOT_RECORD.kernel"
grep -qx initrd-for-saved-work "$BOOT_RECORD.initrd"
grep -q original=1 "$BOOT_RECORD.cmdline"
original=$(sha256sum "$VM_IMAGE" | cut -d' ' -f1)
echo completely-different-template > "$VM_BASE_IMAGE"
bash /entrypoint.sh
grep -qx kernel-for-saved-work "$BOOT_RECORD.kernel"
test "$original" = "$(sha256sum "$VM_IMAGE" | cut -d' ' -f1)"
echo SAVED_DISK_KEEPS_MATCHING_BOOT

# Legacy disks without sidecars are also read from their own /boot.
rm -rf "$VM_IMAGE.boot"
rm "$VM_BASE_IMAGE"
bash /entrypoint.sh
grep -qx kernel-for-saved-work "$BOOT_RECORD.kernel"
test "$original" = "$(sha256sum "$VM_IMAGE" | cut -d' ' -f1)"
test -z "$(find "$work/instances" -name '*.boot.tmp.*' -print -quit)"
echo LEGACY_DISK_READ_WITHOUT_TEMPLATE

# Actual QEMU must retain the run lock and refuse another offline reader.
(
    exec 9>"$VM_IMAGE.run.lock"
    flock -n 9
    exec /usr/bin/qemu-system-x86_64 -machine pc,accel=tcg -m 32 -S -nodefaults \
        -display none -drive "file=$VM_IMAGE,if=none,format=qcow2,id=disk" \
        -qmp "unix:$work/real-qmp.sock,server=on,wait=off"
) &
pid=$!
trap 'kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true' EXIT
for _ in {1..200}; do
    [ -S "$work/real-qmp.sock" ] && break
    kill -0 "$pid"
    sleep 0.02
done
test -S "$work/real-qmp.sock"
if flock -n "$VM_IMAGE.run.lock" true; then
    echo 'QEMU released the disk run lock' >&2
    exit 1
fi
if bash /prepare-boot.sh "$VM_IMAGE"; then
    echo 'offline extraction ignored a running QEMU writer' >&2
    exit 1
fi
test -z "$(find "$work/instances" -name '*.boot.tmp.*' -print -quit)"
test "$original" = "$(sha256sum "$VM_IMAGE" | cut -d' ' -f1)"
echo LIVE_QEMU_LOCKS_PRESERVED
"""


def test_real_disk_boot_files_follow_saved_work_and_refuse_live_image_readers():
    image = os.environ.get("QMP_SMOKE_IMAGE", "ai-desktop-agent-qmp-smoke")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        info = tarfile.TarInfo("guest-boot-probe.sh")
        info.size, info.mode = len(PROBE), 0o500
        tar.addfile(info, io.BytesIO(PROBE))
    client = docker.from_env(timeout=15)
    container = None
    try:
        container = client.containers.create(
            image,
            entrypoint="bash",
            command=["/tmp/guest-boot-probe.sh"],
            network_mode="none",
            cap_drop=["ALL"],
            mem_limit="256m",
            nano_cpus=1_000_000_000,
            pids_limit=64,
            labels={"ai-desktop-agent.smoke": "guest-boot-" + uuid.uuid4().hex},
        )
        container.put_archive("/tmp", archive.getvalue())
        container.start()
        result = container.wait(timeout=60)
        logs = container.logs().decode(errors="replace")
        assert result["StatusCode"] == 0, logs
        assert "SAVED_DISK_KEEPS_MATCHING_BOOT" in logs
        assert "LEGACY_DISK_READ_WITHOUT_TEMPLATE" in logs
        assert "LIVE_QEMU_LOCKS_PRESERVED" in logs
    finally:
        if container is not None:
            container.remove(force=True)
        client.close()
