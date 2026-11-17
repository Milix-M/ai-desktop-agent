#!/bin/bash
# VM Entrypoint — プリビルドされたqcow2イメージをQEMUで起動するだけ
# ゲストはホストの build-vm-image.sh で準備する。
set -euo pipefail

VM_IMAGE="${VM_IMAGE:-/vm/desktop.qcow2}"
VM_BASE_IMAGE="${VM_BASE_IMAGE:-}"
VM_KERNEL="${VM_KERNEL:-}"
VM_INITRD="${VM_INITRD:-}"
VM_MEMORY="${VM_MEMORY:-4096}"
VM_CPUS="${VM_CPUS:-4}"
VM_VNC_PORT="${VM_VNC_PORT:-5900}"
CMDLINE_FILE="${CMDLINE_FILE:-}"
QMP_SOCK="${QMP_SOCK:-/vm/sockets/qmp.sock}"
QGA_SOCK="${QGA_SOCK:-/vm/sockets/qga.sock}"
VNC_DISPLAY=$((VM_VNC_PORT - 5900))
# auto（既定）: /dev/kvm の有無で自動切替。.envでの指定は不要。
# 明示指定時はそれに従う（true=必須、false=TCG）。
USE_KVM="${USE_KVM:-auto}"
ALLOW_TCG_VM="${ALLOW_TCG_VM:-false}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ── QEMU 起動オプション ──

KVM_AVAILABLE=false
if [ -e /dev/kvm ]; then
    KVM_AVAILABLE=true
fi

WANT_KVM="$USE_KVM"
if [ "$WANT_KVM" = "auto" ]; then
    if [ "$KVM_AVAILABLE" = "true" ]; then
        WANT_KVM="true"
    else
        WANT_KVM="false"
    fi
fi

QEMU_ACCEL=()
if [ "$WANT_KVM" = "true" ]; then
    if [ "$KVM_AVAILABLE" != "true" ]; then
        log "ERROR: KVM が要求されましたが /dev/kvm がありません"
        exit 1
    fi
    log "KVM acceleration enabled"
    QEMU_ACCEL=(-enable-kvm -cpu host -smp "$VM_CPUS")
else
    # TCGソフトウェアエミュレーションは実用速度が出ないため、既定では起動を制限する
    if [ "$ALLOW_TCG_VM" != "true" ]; then
        log "ERROR: KVM が利用できないためVM起動を制限します（TCGは遅すぎます）。"
        log "ERROR: コンテナ環境（desktop サービス）を使用してください。"
        log "ERROR: デバッグ目的で起動する場合は ALLOW_TCG_VM=true を設定してください。"
        exit 1
    fi
    log "Using TCG software emulation (no KVM, explicitly allowed)"
    QEMU_ACCEL=(-cpu qemu64 -smp 1)
fi

VM_IMAGE=$(realpath -m -- "$VM_IMAGE")
mkdir -p "$(dirname "$VM_IMAGE")"
# Keep initialization and offline extraction exclusive through the QEMU process.
exec 9>"${VM_IMAGE}.run.lock"
if ! flock -n 9; then
    log "ERROR: VM disk is already starting or running: $VM_IMAGE"
    exit 1
fi

# Fixed VMs initialize a private disk once. Never overwrite saved work on restart.
if [ -n "$VM_BASE_IMAGE" ]; then
    VM_BASE_IMAGE=$(realpath -m -- "$VM_BASE_IMAGE")
    if [ "$VM_IMAGE" = "$VM_BASE_IMAGE" ] || [ "$VM_IMAGE" -ef "$VM_BASE_IMAGE" ]; then
        log "ERROR: VM_BASE_IMAGE must be a regular template file separate from VM_IMAGE"
        exit 1
    fi
    mkdir -p "$(dirname "$VM_IMAGE")"
    (
        flock -x 8
        if [ ! -e "$VM_IMAGE" ]; then
            if [ ! -f "$VM_BASE_IMAGE" ] || [ ! -s "$VM_BASE_IMAGE" ]; then
                log "ERROR: missing or invalid VM template: $VM_BASE_IMAGE"
                exit 1
            fi
            disk_tmp=$(mktemp "${VM_IMAGE}.XXXXXX")
            trap 'rm -f "$disk_tmp"' EXIT
            cp --sparse=always --reflink=auto -- "$VM_BASE_IMAGE" "$disk_tmp"
            mv -- "$disk_tmp" "$VM_IMAGE"
        fi
    ) 8>"${VM_IMAGE}.init.lock"
fi

if [ -z "$VM_KERNEL" ] && [ -z "$VM_INITRD" ]; then
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    timeout --kill-after=5 300 bash "$script_dir/prepare-boot.sh" "$VM_IMAGE"
    VM_KERNEL="${VM_IMAGE}.boot/vmlinuz"
    VM_INITRD="${VM_IMAGE}.boot/initrd.img"
    CMDLINE_FILE="${CMDLINE_FILE:-${VM_IMAGE}.boot/cmdline.txt}"
elif [ -z "$VM_KERNEL" ] || [ -z "$VM_INITRD" ]; then
    log "ERROR: custom boot requires both VM_KERNEL and VM_INITRD"
    exit 1
else
    # Explicit kernel/initrd overrides remain available for custom guest images.
    CMDLINE_FILE="${CMDLINE_FILE:-/vm/cmdline.txt}"
fi
for required_file in "$VM_IMAGE" "$VM_KERNEL" "$VM_INITRD" "$CMDLINE_FILE"; do
    if [ ! -f "$required_file" ] || [ ! -s "$required_file" ]; then
        log "ERROR: missing or invalid VM file: $required_file"
        exit 1
    fi
done

# カーネルコマンドラインを読み込み
CMDLINE="console=ttyS0 root=/dev/vda rw"
if [ -f "$CMDLINE_FILE" ]; then
    CMDLINE=$(cat "$CMDLINE_FILE")
fi

log "Starting VM (VNC:0.0.0.0:$VM_VNC_PORT, RAM:${VM_MEMORY}MB, CPUs:${VM_CPUS}, KVM=$WANT_KVM)"
log "Kernel: $VM_KERNEL, Initrd: $VM_INITRD"
log "Command line: $CMDLINE"

mkdir -p "$(dirname "$QMP_SOCK")" "$(dirname "$QGA_SOCK")"

exec qemu-system-x86_64 \
    "${QEMU_ACCEL[@]}" \
    -m "$VM_MEMORY" \
    -kernel "$VM_KERNEL" \
    -initrd "$VM_INITRD" \
    -append "$CMDLINE" \
    -drive file="$VM_IMAGE",if=virtio,format=qcow2 \
    -vnc "0.0.0.0:$VNC_DISPLAY" \
    -device virtio-net,netdev=net0 \
    -netdev user,id=net0 \
    -serial stdio \
    -display none \
    -chardev socket,path="$QMP_SOCK",server=on,wait=off,id=qmp0 \
    -mon chardev=qmp0,mode=control \
    -chardev socket,path="$QGA_SOCK",server=on,wait=off,id=qga0 \
    -device virtio-serial-pci \
    -device virtserialport,chardev=qga0,name=org.qemu.guest_agent.0
