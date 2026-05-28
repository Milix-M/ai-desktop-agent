#!/bin/bash
# VM Entrypoint — プリビルドされたqcow2イメージをQEMUで起動するだけ
# debootstrapによるイメージビルドはDockerfileのビルド時に完了済み
set -euo pipefail

VM_IMAGE="${VM_IMAGE:-/vm/desktop.qcow2}"
VM_MEMORY="${VM_MEMORY:-2048}"
VM_VNC_PORT="${VM_VNC_PORT:-5900}"
CMDLINE_FILE="${CMDLINE_FILE:-/vm/cmdline.txt}"
VNC_DISPLAY=$((VM_VNC_PORT - 5900))
USE_KVM="${USE_KVM:-false}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ── QEMU 起動オプション ──

QEMU_ACCEL=()
if [ "$USE_KVM" = "true" ] && [ -e /dev/kvm ]; then
    log "KVM acceleration enabled"
    QEMU_ACCEL=(-enable-kvm -cpu host -smp 2)
else
    log "Using TCG software emulation (no KVM)"
    QEMU_ACCEL=(-cpu qemu64 -smp 1)
fi

# カーネルコマンドラインを読み込み
CMDLINE="console=ttyS0 root=/dev/vda rw"
if [ -f "$CMDLINE_FILE" ]; then
    CMDLINE=$(cat "$CMDLINE_FILE")
fi

log "Starting VM (VNC:0.0.0.0:$VM_VNC_PORT, RAM:${VM_MEMORY}MB, KVM=$USE_KVM)"
log "Kernel: /vm/vmlinuz, Initrd: /vm/initrd.img"
log "Command line: $CMDLINE"

exec qemu-system-x86_64 \
    "${QEMU_ACCEL[@]}" \
    -m "$VM_MEMORY" \
    -kernel /vm/vmlinuz \
    -initrd /vm/initrd.img \
    -append "$CMDLINE" \
    -drive file="$VM_IMAGE",if=virtio,format=qcow2 \
    -vnc "0.0.0.0:$VNC_DISPLAY" \
    -device virtio-net,netdev=net0 \
    -netdev user,id=net0 \
    -display none
