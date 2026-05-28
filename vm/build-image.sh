#!/bin/bash
# build-image.sh — Dockerfile builder stage内で、現在のrootfsからqcow2イメージを作成
# debootstrap不要。builder自身（amd64 Ubuntu）がそのままVMのrootfsになる
set -euo pipefail

IMAGE_DIR="/vm"
IMAGE_FILE="$IMAGE_DIR/desktop.qcow2"
DISK_IMG="$IMAGE_DIR/disk.raw"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "Creating rootfs disk image from current filesystem"

# ── カーネルとinitrdを抽出 ──
KERNEL_VER=$(ls /lib/modules | head -1)
log "Kernel version: $KERNEL_VER"
cp "/boot/vmlinuz-$KERNEL_VER" "$IMAGE_DIR/vmlinuz"
cp "/boot/initrd.img-$KERNEL_VER" "$IMAGE_DIR/initrd.img"
echo "console=ttyS0 root=/dev/vda rw" > "$IMAGE_DIR/cmdline.txt"

# ── ファイルシステムサイズ計算 ──
# /proc, /sys, /dev, /vm, /tmp, /run, /var/cache, /var/lib/apt/lists を除外
ROOTFS_SIZE_KB=$(du -sk \
    --exclude=/proc \
    --exclude=/sys \
    --exclude=/dev \
    --exclude=/vm \
    --exclude=/tmp \
    --exclude=/run \
    --exclude=/var/cache/apt \
    --exclude=/var/lib/apt/lists \
    --exclude=/build-image.sh \
    / 2>/dev/null | cut -f1)
ROOTFS_SIZE_MB=$((ROOTFS_SIZE_KB / 1024))
DISK_SIZE_MB=$((ROOTFS_SIZE_MB + 2048))
log "Rootfs size: ${ROOTFS_SIZE_MB}MB, Disk size: ${DISK_SIZE_MB}MB"

# ── ext4 ディスクイメージ作成 ──
dd if=/dev/zero of="$DISK_IMG" bs=1M count="$DISK_SIZE_MB" status=progress
mkfs.ext4 -F "$DISK_IMG"

mkdir -p /mnt/disk
mount "$DISK_IMG" /mnt/disk

# 必要なディレクトリを先に作成
mkdir -p /mnt/disk/{proc,sys,dev,tmp,run,media,mnt}

# rootfsをコピー (proc/sys/dev/vm/tmp/run 以外)
log "Copying rootfs to disk image..."
rsync -a \
    --exclude=/proc \
    --exclude=/sys \
    --exclude=/dev \
    --exclude=/vm \
    --exclude=/tmp \
    --exclude=/run \
    --exclude=/var/cache/apt \
    --exclude=/build-image.sh \
    --exclude=/mnt \
    / /mnt/disk/

# fstab作成
echo "/dev/vda / ext4 defaults 0 1" > /mnt/disk/etc/fstab

umount /mnt/disk

# ── qcow2 変換 ──
log "Converting to qcow2 format"
qemu-img convert -f raw -O qcow2 "$DISK_IMG" "$IMAGE_FILE"
rm -f "$DISK_IMG"

log "Done! qcow2 size: $(du -sh "$IMAGE_FILE" | cut -f1)"
log "Kernel: $(du -sh "$IMAGE_DIR/vmlinuz" | cut -f1)"
log "Initrd: $(du -sh "$IMAGE_DIR/initrd.img" | cut -f1)"
