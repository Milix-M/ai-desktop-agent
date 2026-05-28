#!/bin/bash
# prepare-image.sh — Dockerfile内で呼ばれる。qcow2イメージをビルドする。
# debootstrap → KDE Plasmaインストール → 設定 → ext4イメージ → qcow2変換
# クロスアーキテクチャ（ARM64ホスト→AMD64ゲスト）対応
set -euo pipefail

ROOTFS="/rootfs"
IMAGE_DIR="/vm"
IMAGE_FILE="$IMAGE_DIR/desktop.qcow2"
IMAGE_SIZE="${IMAGE_SIZE:-20G}"
DISK_IMG="$IMAGE_DIR/disk.raw"
TARGET_ARCH="${TARGET_ARCH:-amd64}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ── Step 1: debootstrap で Ubuntu Noble の最小rootfsを作成 ──
log "Step 1: debootstrap Ubuntu Noble ($TARGET_ARCH)"

# クロスアーキテクチャの場合: --foreign で1段階目だけ実行
DEBOOTSTRAP_ARGS="--variant=minbase --include=systemd,apt,ubuntu-keyring"
if [ "$(uname -m)" != "x86_64" ] && [ "$TARGET_ARCH" = "amd64" ]; then
    log "Cross-architecture: host=$(uname -m), target=$TARGET_ARCH"
    DEBOOTSTRAP_ARGS="$DEBOOTSTRAP_ARGS --foreign --arch=$TARGET_ARCH"
    # ARM64→AMD64用のQEMU静的バイナリをコピー（debootstrap second stage用）
    QEMU_STATIC=$(which qemu-x86_64-static 2>/dev/null || echo "")
    if [ -n "$QEMU_STATIC" ]; then
        mkdir -p "$ROOTFS/usr/bin"
        cp "$QEMU_STATIC" "$ROOTFS/usr/bin/"
    fi
else
    DEBOOTSTRAP_ARGS="$DEBOOTSTRAP_ARGS --arch=$TARGET_ARCH"
fi

debootstrap $DEBOOTSTRAP_ARGS noble "$ROOTFS" http://ports.ubuntu.com/ubuntu-ports

# foreignの場合はsecond stageを実行
if [ -f "$ROOTFS/debootstrap/debootstrap" ]; then
    log "Running debootstrap second stage..."
    # QEMU静的バイナリでsecond stageを実行
    if [ -n "${QEMU_STATIC:-}" ] && [ -f "$QEMU_STATIC" ]; then
        cp "$QEMU_STATIC" "$ROOTFS/usr/bin/"
    fi
    chroot "$ROOTFS" /debootstrap/debootstrap --second-stage
fi

# ── Step 2: chroot の準備とパッケージインストール ──
log "Step 2: Installing kernel and KDE Plasma"

# QEMU静的バイナリをchroot内にコピー（クロスアーキテクチャ用）
if [ -n "${QEMU_STATIC:-}" ] && [ -f "${QEMU_STATIC:-}" ]; then
    cp "$QEMU_STATIC" "$ROOTFS/usr/bin/"
fi

# 必要なマウントを設定
mount -t proc proc "$ROOTFS/proc"
mount -t sysfs sys "$ROOTFS/sys"
mount -o bind /dev "$ROOTFS/dev"
mount -o bind /dev/pts "$ROOTFS/dev/pts"

# DNS設定をコピー
cp /etc/resolv.conf "$ROOTFS/etc/resolv.conf"

# パッケージをインストール
chroot "$ROOTFS" /bin/bash -e << 'CHROOT_EOF'
export DEBIAN_FRONTEND=noninteractive

# パッケージリスト更新
apt-get update

# 言語パックの警告を抑制
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

# クリーンアップ
apt-get clean
rm -rf /var/lib/apt/lists/*
CHROOT_EOF

# ── Step 3: agent ユーザーと自動ログイン設定 ──
log "Step 3: Creating agent user and autologin"

# agentユーザー作成
chroot "$ROOTFS" useradd -m -s /bin/bash -G sudo agent
echo 'agent:agent' | chroot "$ROOTFS" chpasswd
echo 'agent ALL=(ALL) NOPASSWD:ALL' > "$ROOTFS/etc/sudoers.d/agent"

# getty@tty1 自動ログイン
mkdir -p "$ROOTFS/etc/systemd/system/getty@tty1.service.d"
cat > "$ROOTFS/etc/systemd/system/getty@tty1.service.d/autologin.conf" << 'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin agent --noclear %I $TERM
EOF

# .profile: tty1ならKDEを起動
cat > "$ROOTFS/home/agent/.profile" << 'EOF'
# ~/.profile: コンソールログイン時にKDE Plasmaを自動起動
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    exec startplasma-x11
fi
EOF
chroot "$ROOTFS" chown agent:agent /home/agent/.profile

# systemd-resolved を有効化（DNS用）
chroot "$ROOTFS" systemctl enable systemd-resolved

# getty@tty1 を有効化
rm -f "$ROOTFS/etc/systemd/system/getty.target.wants/getty@tty1.service"
chroot "$ROOTFS" systemctl enable getty@tty1

# ── Step 4: カーネルとinitrdを抽出（QEMUの -kernel/-initrd 用）──
log "Step 4: Extracting kernel and initrd"

# インストールされたカーネルを探す
KERNEL_VER=$(ls "$ROOTFS/lib/modules" | head -1)
cp "$ROOTFS/boot/vmlinuz-$KERNEL_VER" "$IMAGE_DIR/vmlinuz"
cp "$ROOTFS/boot/initrd.img-$KERNEL_VER" "$IMAGE_DIR/initrd.img"

# カーネル起動パラメータをファイルに保存
echo "console=ttyS0 root=/dev/vda rw" > "$IMAGE_DIR/cmdline.txt"

# ── Step 5: rootfs を ext4 ディスクイメージに変換 ──
log "Step 5: Creating ext4 disk image ($IMAGE_SIZE)"

# マウント解除
umount "$ROOTFS/dev/pts" || true
umount "$ROOTFS/dev" || true
umount "$ROOTFS/sys" || true
umount "$ROOTFS/proc" || true

# rootfsのサイズを取得（MB単位）
ROOTFS_SIZE_MB=$(du -sm "$ROOTFS" | cut -f1)
# 余裕を持って+2GB
DISK_SIZE_MB=$((ROOTFS_SIZE_MB + 2048))

log "Rootfs size: ${ROOTFS_SIZE_MB}MB, Disk size: ${DISK_SIZE_MB}MB"

# 空のディスクイメージ作成
dd if=/dev/zero of="$DISK_IMG" bs=1M count="$DISK_SIZE_MB" status=progress

# ext4でフォーマット
mkfs.ext4 -F "$DISK_IMG"

# rootfsの内容をコピー
log "Copying rootfs to disk image..."
mkdir -p /mnt/disk
mount "$DISK_IMG" /mnt/disk
cp -a "$ROOTFS"/* /mnt/disk/
umount /mnt/disk

# ── Step 6: ext4 → qcow2 変換 ──
log "Step 6: Converting to qcow2"
qemu-img convert -f raw -O qcow2 "$DISK_IMG" "$IMAGE_FILE"
rm -f "$DISK_IMG"

# サイズ表示
log "Done! qcow2 size: $(du -sh "$IMAGE_FILE" | cut -f1)"
log "Kernel: $(ls -la "$IMAGE_DIR/vmlinuz" | awk '{print $5}') bytes"
log "Initrd: $(ls -la "$IMAGE_DIR/initrd.img" | awk '{print $5}') bytes"
