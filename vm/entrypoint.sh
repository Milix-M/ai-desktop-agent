#!/bin/bash
# VM Entrypoint — 初回起動時にUbuntu Cloud Imageをダウンロードし、
# cloud-initでデスクトップ環境をセットアップした後、QEMUを起動する。
# KVMが使えない環境では USE_KVM=false でTCGエミュレーションに切り替え。
set -euo pipefail

VM_IMAGE="${VM_IMAGE:-/vm/desktop.qcow2}"
VM_MEMORY="${VM_MEMORY:-2048}"
VM_VNC_PORT="${VM_VNC_PORT:-5900}"
CLOUD_IMAGE_URL="${CLOUD_IMAGE_URL:-https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img}"
SEED_IMAGE="/vm/seed.img"
BACKING_IMAGE="/vm/cloudimg.qcow2"
VNC_DISPLAY=$((VM_VNC_PORT - 5900))
USE_KVM="${USE_KVM:-false}"

log()  { echo "[$(date '+%H:%M:%S')] $*"; }

# ── QEMU 起動オプション ──────────────────────────────

QEMU_ACCEL=()
if [ "$USE_KVM" = "true" ] && [ -e /dev/kvm ]; then
    log "KVM acceleration enabled"
    QEMU_ACCEL=(-enable-kvm -cpu host -smp 2)
else
    log "Using TCG software emulation (no KVM)"
    QEMU_ACCEL=(-cpu qemu64 -smp 1)
fi

# ── 初回起動: VMイメージの準備 ──────────────────────

if [ ! -f "$VM_IMAGE" ]; then
    log "=== First run: preparing VM image ==="

    # クラウドイメージをダウンロード
    log "Downloading cloud image..."
    wget -q --show-progress -O "$BACKING_IMAGE" "$CLOUD_IMAGE_URL"

    # バッキングファイルからオーバーレイを作成
    log "Creating overlay image (30G)..."
    qemu-img create -f qcow2 -F qcow2 -b "$BACKING_IMAGE" "$VM_IMAGE" 30G

    # cloud-init seed イメージ作成
    log "Creating cloud-init seed..."
    cloud-localds "$SEED_IMAGE" /cloud-init/user-data /cloud-init/meta-data

    # 初回起動（cloud-init実行用。-display none + background &）
    log "Booting VM for cloud-init provisioning (TCG requires 30-60 min)..."
    qemu-system-x86_64 \
        "${QEMU_ACCEL[@]}" \
        -m "$VM_MEMORY" \
        -drive file="$VM_IMAGE",if=virtio \
        -drive file="$SEED_IMAGE",if=virtio,format=raw \
        -vnc "0.0.0.0:$VNC_DISPLAY" \
        -device virtio-net,netdev=net0 \
        -netdev user,id=net0 \
        -display none &

    QEMU_PID=$!
    log "QEMU PID=$QEMU_PID"

    # VNC が開くのを待つ
    log "Waiting for VNC to become available..."
    for i in $(seq 1 360); do
        if echo | nc -z localhost "$VM_VNC_PORT" 2>/dev/null; then
            log "VNC port $VM_VNC_PORT is open!"
            break
        fi
        if [ $((i % 60)) -eq 0 ]; then
            log "... still waiting (${i}0s elapsed)"
        fi
        sleep 10
    done

    # cloud-init パッケージインストール完了を待つ（TCGは15分）
    log "Waiting 15 minutes for cloud-init to install KDE Plasma..."
    sleep 900

    # VM を停止
    log "Shutting down provisioning VM (PID=$QEMU_PID)..."
    kill "$QEMU_PID" 2>/dev/null || true
    wait "$QEMU_PID" 2>/dev/null || true
    sleep 3

    # seed イメージを削除（2回目以降不要）
    rm -f "$SEED_IMAGE"

    log "=== VM image ready ==="
fi

# ── 本番起動 ────────────────────────────────────────

log "Starting VM (VNC:0.0.0.0:$VM_VNC_PORT, RAM:${VM_MEMORY}MB, KVM=$USE_KVM)..."

exec qemu-system-x86_64 \
    "${QEMU_ACCEL[@]}" \
    -m "$VM_MEMORY" \
    -drive file="$VM_IMAGE",if=virtio \
    -vnc "0.0.0.0:$VNC_DISPLAY" \
    -device virtio-net,netdev=net0 \
    -netdev user,id=net0 \
    -display none
