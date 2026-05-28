#!/bin/bash
# prepare-image.sh — Dockerfile builder stage内で実行される。
# debootstrap → KDE Plasmaインストール → 設定 → ext4イメージ → qcow2変換
# builder stage は --platform=linux/amd64 で動作するため、常にネイティブamd64環境
set -euo pipefail

ROOTFS="/rootfs"
IMAGE_DIR="/vm"
IMAGE_FILE="$IMAGE_DIR/desktop.qcow2"
DISK_IMG="$IMAGE_DIR/disk.raw"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ── Step 1: debootstrap ──

log "Step 1: debootstrap Ubuntu Noble (amd64)"

debootstrap --variant=minbase --include=systemd,apt,ubuntu-keyring \
    --arch=amd64 noble "$ROOTFS" http://archive.ubuntu.com/ubuntu

# ── Step 2: パッケージインストール ──

log "Step 2: Installing kernel and KDE Plasma"

# 必要なマウントをchroot用に設定
mount -t proc proc "$ROOTFS/proc"
mount -t sysfs sys "$ROOTFS/sys"
mount -o bind /dev "$ROOTFS/dev"
mount -o bind /dev/pts "$ROOTFS/dev/pts"
cp /etc/resolv.conf "$ROOTFS/etc/resolv.conf"

chroot "$ROOTFS" /bin/bash -euo pipefail << 'CHROOT_EOF'
export DEBIAN_FRONTEND=noninteractive
apt-get update

# 言語パック
apt-get install -y --no-install-recommends locales
locale-gen en_US.UTF-8
update-locale LANG=en_US.UTF-8

# カーネル + 基本ツール
apt-get install -y --no-install-recommends \
    linux-image-generic \
    initramfs-tools \
    udev \
    dbus \
    network-manager \
    sudo \
    curl \
    wget

# KDE Plasma 最小デスクトップ
apt-get install -y --no-install-recommends \
    kde-plasma-desktop \
    konsole \
    firefox \
    xdotool \
    wmctrl \
    xauth \
    dolphin

apt-get clean
rm -rf /var/lib/apt/lists/*
CHROOT_EOF

# ── Step 3: agent ユーザーと自動ログイン設定 ──

log "Step 3: Creating agent user and autologin"

chroot "$ROOTFS" /bin/bash -euo pipefail << 'CHROOT_EOF'
useradd -m -s /bin/bash -G sudo agent
echo "agent:agent" | chpasswd
echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent
chown agent:agent /home/agent/.profile
systemctl enable systemd-resolved
systemctl enable getty@tty1
CHROOT_EOF

mkdir -p "$ROOTFS/etc/systemd/system/getty@tty1.service.d"
cat > "$ROOTFS/etc/systemd/system/getty@tty1.service.d/autologin.conf" << 'UNIT_EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin agent --noclear %I $TERM
UNIT_EOF

cat > "$ROOTFS/home/agent/.profile" << 'PROFILE_EOF'
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    exec startplasma-x11
fi
PROFILE_EOF

chroot "$ROOTFS" chown agent:agent /home/agent/.profile

# ── Step 4: カーネルとinitrdを抽出 ──

log "Step 4: Extracting kernel and initrd"

KERNEL_VER=$(ls "$ROOTFS/lib/modules" | head -1)
cp "$ROOTFS/boot/vmlinuz-$KERNEL_VER" "$IMAGE_DIR/vmlinuz"
cp "$ROOTFS/boot/initrd.img-$KERNEL_VER" "$IMAGE_DIR/initrd.img"
echo "console=ttyS0 root=/dev/vda rw" > "$IMAGE_DIR/cmdline.txt"

# ── Step 5: ext4 ディスクイメージ作成 ──

log "Step 5: Creating ext4 disk image"

umount "$ROOTFS/dev/pts" || true
umount "$ROOTFS/dev" || true
umount "$ROOTFS/sys" || true
umount "$ROOTFS/proc" || true

ROOTFS_SIZE_MB=$(du -sm "$ROOTFS" | cut -f1)
DISK_SIZE_MB=$((ROOTFS_SIZE_MB + 2048))
log "Rootfs size: ${ROOTFS_SIZE_MB}MB, Disk size: ${DISK_SIZE_MB}MB"

dd if=/dev/zero of="$DISK_IMG" bs=1M count="$DISK_SIZE_MB" status=progress
mkfs.ext4 -F "$DISK_IMG"

mkdir -p /mnt/disk
mount "$DISK_IMG" /mnt/disk
cp -a "$ROOTFS"/* /mnt/disk/
umount /mnt/disk

# ── Step 6: qcow2 変換 ──

log "Step 6: Converting to qcow2"
qemu-img convert -f raw -O qcow2 "$DISK_IMG" "$IMAGE_FILE"
rm -f "$DISK_IMG"

log "Done! qcow2 size: $(du -sh "$IMAGE_FILE" | cut -f1)"
log "Kernel: $(ls -la "$IMAGE_DIR/vmlinuz" | awk '{print $5}') bytes"
log "Initrd: $(ls -la "$IMAGE_DIR/initrd.img" | awk '{print $5}') bytes"
