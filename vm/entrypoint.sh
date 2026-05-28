#!/bin/bash
# VM Entrypoint — 初回起動時にUbuntu Cloud Imageをダウンロードし、
# cloud-initでデスクトップ環境をセットアップした後、QEMU/KVMを起動する。
set -euo pipefail

VM_IMAGE="${VM_IMAGE:-/vm/desktop.qcow2}"
VM_MEMORY="${VM_MEMORY:-4096}"
VM_VNC_PORT="${VM_VNC_PORT:-5900}"
CLOUD_IMAGE_URL="${CLOUD_IMAGE_URL:-https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img}"
SEED_IMAGE="/vm/seed.img"
BACKING_IMAGE="/vm/cloudimg.qcow2"
VNC_DISPLAY=$((VM_VNC_PORT - 5900))

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
log_err() { echo "[$(date '+%H:%M:%S')] ERROR: $*" >&2; }

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

    # 初回起動（cloud-init実行のため、snapshotなし）
    log "Booting VM for cloud-init provisioning (5-10 min, KDE Plasma)..."
    qemu-system-x86_64 \
        -enable-kvm \
        -cpu host \
        -m "$VM_MEMORY" \
        -smp 2 \
        -drive file="$VM_IMAGE",if=virtio \
        -drive file="$SEED_IMAGE",if=virtio,format=raw \
        -vnc "0.0.0.0:$VNC_DISPLAY" \
        -device virtio-net,netdev=net0 \
        -netdev user,id=net0 \
        -nographic \
        -daemonize

    QEMU_PID=$!

    # cloud-init 完了を待つ（VNCが開く → さらに120秒でパッケージインストール完了）
    log "Waiting for cloud-init to finish..."
    for i in $(seq 1 90); do
        if echo | nc -z localhost "$VM_VNC_PORT" 2>/dev/null; then
            log "VNC port $VM_VNC_PORT is open, waiting for cloud-init completion..."
            sleep 240
            break
        fi
        sleep 10
    done

    # VM を停止
    log "Shutting down provisioning VM..."
    kill "$QEMU_PID" 2>/dev/null || true
    wait "$QEMU_PID" 2>/dev/null || true
    sleep 3

    # seed イメージを削除（2回目以降不要）
    rm -f "$SEED_IMAGE"

    log "=== VM image ready ==="
fi

# ── 本番起動 ────────────────────────────────────────

log "Starting VM (VNC:0.0.0.0:$VM_VNC_PORT, RAM:${VM_MEMORY}MB)..."

exec qemu-system-x86_64 \
    -enable-kvm \
    -cpu host \
    -m "$VM_MEMORY" \
    -smp 2 \
    -drive file="$VM_IMAGE",if=virtio \
    -vnc "0.0.0.0:$VNC_DISPLAY" \
    -device virtio-net,netdev=net0 \
    -netdev user,id=net0 \
    -nographic
