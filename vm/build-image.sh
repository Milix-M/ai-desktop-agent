#!/bin/bash
# build-image.sh — Dockerfile builder stage内で、現在のrootfsからqcow2イメージを作成
# mount不要。mkfs.ext4 -d でrootfsを直接書き込む
set -euo pipefail

IMAGE_DIR="/vm"
IMAGE_FILE="$IMAGE_DIR/desktop.qcow2"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "Creating rootfs disk image from current filesystem"

# ── カーネルとinitrdを抽出 ──
KERNEL_VER=$(ls /lib/modules | head -1)
log "Kernel version: $KERNEL_VER"
cp "/boot/vmlinuz-$KERNEL_VER" "$IMAGE_DIR/vmlinuz"
cp "/boot/initrd.img-$KERNEL_VER" "$IMAGE_DIR/initrd.img"
echo "console=ttyS0 root=/dev/vda rw" > "$IMAGE_DIR/cmdline.txt"

# ── ファイルシステムサイズ計算 ──
ROOTFS_SIZE_KB=$(du -sk \
    --exclude=/proc \
    --exclude=/sys \
    --exclude=/dev \
    --exclude=/vm \
    --exclude=/tmp \
    --exclude=/run \
    / 2>/dev/null | cut -f1)
ROOTFS_SIZE_MB=$((ROOTFS_SIZE_KB / 1024))
DISK_SIZE_MB=$((ROOTFS_SIZE_MB + 2048))
log "Rootfs size: ${ROOTFS_SIZE_MB}MB, Disk size: ${DISK_SIZE_MB}MB"

# ── 一時tarでクリーンなrootfsを準備 ──
# mkfs.ext4 -d に除外リストがないため、先に除外してtarで固める
log "Preparing rootfs tarball..."
tar cpf /tmp/rootfs.tar \
    --exclude=/proc \
    --exclude=/sys \
    --exclude=/dev \
    --exclude=/vm \
    --exclude=/tmp \
    --exclude=/run \
    --exclude=/var/cache/apt \
    --exclude=/build-image.sh \
    --exclude=/tmp/rootfs.tar \
    -C / . 2>/dev/null

mkdir -p /tmp/rootfs-target
tar xpf /tmp/rootfs.tar -C /tmp/rootfs-target
rm -f /tmp/rootfs.tar

# 必要な空ディレクトリを作成
mkdir -p /tmp/rootfs-target/{proc,sys,dev,tmp,run,media,mnt}

# fstab作成
echo "/dev/vda / ext4 defaults 0 1" > /tmp/rootfs-target/etc/fstab

# ── ext4 ディスクイメージ作成 (mount不要！) ──
log "Creating ext4 disk image with mkfs.ext4 -d (no mount needed)..."
dd if=/dev/zero of="$IMAGE_DIR/disk.raw" bs=1M count="$DISK_SIZE_MB" status=progress
mkfs.ext4 -F -d /tmp/rootfs-target "$IMAGE_DIR/disk.raw"

# 後片付け
rm -rf /tmp/rootfs-target

# ── qcow2 変換 ──
log "Converting to qcow2 format"
qemu-img convert -f raw -O qcow2 "$IMAGE_DIR/disk.raw" "$IMAGE_FILE"
rm -f "$IMAGE_DIR/disk.raw"

log "Done! qcow2 size: $(du -sh "$IMAGE_FILE" | cut -f1)"
log "Kernel: $(du -sh "$IMAGE_DIR/vmlinuz" | cut -f1)"
log "Initrd: $(du -sh "$IMAGE_DIR/initrd.img" | cut -f1)"
