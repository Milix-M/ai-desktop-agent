#!/bin/bash
# build-image.sh — builder内のrootfsからqcow2イメージを作成
# mount不要。mkfs.ext4 -d で直接書き込み + tarパイプで効率化
set -euo pipefail

IMAGE_DIR="/vm"
TARGET="/tmp/rootfs-target"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "Creating rootfs disk image from current filesystem"

# ── カーネルとinitrdを抽出 ──
KERNEL_VER=$(ls /lib/modules | head -1)
log "Kernel version: $KERNEL_VER"
cp "/boot/vmlinuz-$KERNEL_VER" "$IMAGE_DIR/vmlinuz"
cp "/boot/initrd.img-$KERNEL_VER" "$IMAGE_DIR/initrd.img"
echo "console=ttyS0 root=/dev/vda rw" > "$IMAGE_DIR/cmdline.txt"

# ── rootfsを別ディレクトリにコピー（tarパイプで中間ファイルなし） ──
log "Copying rootfs (tar pipe, no intermediate file)..."
mkdir -p "$TARGET"
cd /
tar cf - \
    --exclude=proc --exclude=sys \
    --exclude=vm --exclude=tmp --exclude=run \
    --exclude=mnt \
    --exclude=build-image.sh \
    . 2>/dev/null | tar xf - -C "$TARGET"

# 空のマウントポイント作成
mkdir -p "$TARGET"/{proc,sys,dev,tmp,run,mnt}

# fstab
echo "/dev/vda / ext4 defaults 0 1" > "$TARGET/etc/fstab"

# ── サイズ計算と ext4 作成 ──
ROOTFS_SIZE_MB=$(du -sm "$TARGET" | cut -f1)
DISK_SIZE_MB=$((ROOTFS_SIZE_MB + 2048))
log "Rootfs: ${ROOTFS_SIZE_MB}MB → Disk: ${DISK_SIZE_MB}MB"

dd if=/dev/zero of="$IMAGE_DIR/disk.raw" bs=1M count="$DISK_SIZE_MB" status=progress
mkfs.ext4 -F -d "$TARGET" "$IMAGE_DIR/disk.raw"

# 後片付け
rm -rf "$TARGET"

# ── qcow2 変換 ──
log "Converting to qcow2..."
qemu-img convert -f raw -O qcow2 "$IMAGE_DIR/disk.raw" "$IMAGE_DIR/desktop.qcow2"
rm -f "$IMAGE_DIR/disk.raw"

log "Done! qcow2: $(du -sh "$IMAGE_DIR/desktop.qcow2" | cut -f1)"
log "Kernel: $(du -sh "$IMAGE_DIR/vmlinuz" | cut -f1)"
log "Initrd: $(du -sh "$IMAGE_DIR/initrd.img" | cut -f1)"
