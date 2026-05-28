#!/bin/bash
# prepare-image.sh — Dockerfile内で呼ばれる。qcow2イメージをビルドする。
# debootstrap → KDE Plasmaインストール → 設定 → ext4イメージ → qcow2変換
# クロスアーキテクチャ（ARM64ホスト→AMD64ゲスト）対応
set -euo pipefail

ROOTFS="/rootfs"
IMAGE_DIR="/vm"
IMAGE_FILE="$IMAGE_DIR/desktop.qcow2"
DISK_IMG="$IMAGE_DIR/disk.raw"
TARGET_ARCH="${TARGET_ARCH:-amd64}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ── クロスアーキテクチャ検出とセットアップ ──

CROSS_ARCH=false
QEMU_STATIC=""
if [ "$(uname -m)" != "x86_64" ] && [ "$TARGET_ARCH" = "amd64" ]; then
    QEMU_STATIC=$(which qemu-x86_64-static 2>/dev/null || echo "")
    if [ -z "$QEMU_STATIC" ]; then
        log "ERROR: qemu-x86_64-static not found, need qemu-user-static package"
        exit 1
    fi
    CROSS_ARCH=true
    log "Cross-architecture: host=$(uname -m), target=$TARGET_ARCH"
fi

# chrootラッパー: クロスアーキテクチャ時はqemu-static経由で実行
if $CROSS_ARCH; then
    chroot_cmd() {
        local script="$1"
        # QEMU静的バイナリをchroot内に配置
        cp "$QEMU_STATIC" "$ROOTFS/usr/bin/"
        # スクリプトファイルをchroot内に書き込んで実行
        echo "$script" > "$ROOTFS/tmp/_setup.sh"
        chroot "$ROOTFS" /usr/bin/qemu-x86_64-static /bin/sh /tmp/_setup.sh
        rm -f "$ROOTFS/tmp/_setup.sh"
    }
else
    chroot_cmd() {
        local script="$1"
        echo "$script" > "$ROOTFS/tmp/_setup.sh"
        chroot "$ROOTFS" /bin/sh /tmp/_setup.sh
        rm -f "$ROOTFS/tmp/_setup.sh"
    }
fi

# ── Step 1: debootstrap ──

log "Step 1: debootstrap Ubuntu Noble ($TARGET_ARCH)"

DEBOOTSTRAP_ARGS="--variant=minbase --include=systemd,apt,ubuntu-keyring"
if $CROSS_ARCH; then
    DEBOOTSTRAP_ARGS="$DEBOOTSTRAP_ARGS --foreign --arch=$TARGET_ARCH"
else
    DEBOOTSTRAP_ARGS="$DEBOOTSTRAP_ARGS --arch=$TARGET_ARCH"
fi

# amd64(x86_64)はarchive.ubuntu.com、それ以外はports.ubuntu.com
if [ "$TARGET_ARCH" = "amd64" ] || [ "$TARGET_ARCH" = "i386" ]; then
    MIRROR="http://archive.ubuntu.com/ubuntu"
else
    MIRROR="http://ports.ubuntu.com/ubuntu-ports"
fi

debootstrap $DEBOOTSTRAP_ARGS noble "$ROOTFS" "$MIRROR"

# foreignの場合はsecond stageを実行
if [ -f "$ROOTFS/debootstrap/debootstrap" ]; then
    log "Running debootstrap second stage..."
    if $CROSS_ARCH; then
        cp "$QEMU_STATIC" "$ROOTFS/usr/bin/"
        chroot "$ROOTFS" /usr/bin/qemu-x86_64-static /bin/sh /debootstrap/debootstrap --second-stage
    else
        chroot "$ROOTFS" /debootstrap/debootstrap --second-stage
    fi
fi

# ── Step 2: パッケージインストール ──

log "Step 2: Installing kernel and KDE Plasma"

# 必要なマウントを設定
mount -t proc proc "$ROOTFS/proc"
mount -t sysfs sys "$ROOTFS/sys"
mount -o bind /dev "$ROOTFS/dev"
mount -o bind /dev/pts "$ROOTFS/dev/pts"
cp /etc/resolv.conf "$ROOTFS/etc/resolv.conf"

chroot_cmd '
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
'

# ── Step 3: agent ユーザーと自動ログイン設定 ──

log "Step 3: Creating agent user and autologin"

chroot_cmd '
useradd -m -s /bin/bash -G sudo agent
echo "agent:agent" | chpasswd
echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent
'

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

chroot_cmd '
chown agent:agent /home/agent/.profile
systemctl enable systemd-resolved
systemctl enable getty@tty1
'

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
