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

    # cloud-init 完了を待つ。
    # TCGは非常に遅いので、VNC解像度の変化でデスクトップ起動を検出する。
    CLOUD_INIT_TIMEOUT=3600
    if [ "$USE_KVM" = "true" ] && [ -e /dev/kvm ]; then
        CLOUD_INIT_TIMEOUT=600
    fi
    log "Waiting for cloud-init to finish (max ${CLOUD_INIT_TIMEOUT}s)..."
    POLL_INTERVAL=30
    ELAPSED=0
    while [ $ELAPSED -lt $CLOUD_INIT_TIMEOUT ]; do
        RESOLUTION=$(timeout 3 python3 -c "
import socket, struct
try:
    s = socket.socket(); s.settimeout(2)
    s.connect(('localhost', $VM_VNC_PORT))
    s.recv(12)
    s.send(b'RFB 003.008\n')
    n = struct.unpack('B', s.recv(1))[0]
    if n > 0: s.recv(n)
    s.send(b'\x01')
    s.recv(4)
    s.send(b'\x01')
    w = struct.unpack('>H', s.recv(2))[0]
    h = struct.unpack('>H', s.recv(2))[0]
    s.close()
    print(f'{w}x{h}')
except:
    print('0x0')
" 2>/dev/null || echo "0x0")
        log "  VNC resolution: $RESOLUTION (elapsed: ${ELAPSED}s)"

        if [ "$RESOLUTION" != "720x400" ] && [ "$RESOLUTION" != "0x0" ]; then
            log "Desktop detected! Resolution changed to $RESOLUTION"
            break
        fi

        sleep $POLL_INTERVAL
        ELAPSED=$((ELAPSED + POLL_INTERVAL))
    done

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
