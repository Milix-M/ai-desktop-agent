#!/bin/bash
# build-vm-image.sh — ホスト上で debootstrap → qcow2 を構築
# x86_64 / ARM64 両対応
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="/tmp/vm-rootfs-build"
OUTPUT_DIR="$SCRIPT_DIR"
IMAGE_NAME="desktop.qcow2"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

cleanup() {
    log "Cleaning up..."
    for mp in /proc /sys /dev; do
        umount "$TARGET$mp" 2>/dev/null || true
    done
    rm -rf "$TARGET"
}
trap cleanup EXIT

# ── アーキテクチャ判定 ──
HOST_ARCH=$(uname -m)
case "$HOST_ARCH" in
    x86_64|amd64)  TARGET_ARCH=amd64; NEED_QEMU=false ;;
    aarch64|arm64) TARGET_ARCH=amd64; NEED_QEMU=true ;;
    *) echo "Error: Unsupported arch: $HOST_ARCH"; exit 1 ;;
esac

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: Must run as root (sudo)"
    exit 1
fi

if $NEED_QEMU; then
    if [ ! -f /proc/sys/fs/binfmt_misc/qemu-x86_64 ]; then
        echo "Installing qemu-user-static for cross-arch build..."
        apt-get update -qq && apt-get install -y -qq qemu-user-static
        update-binfmts --enable qemu-x86_64
    fi
fi

# 依存チェック
for cmd in debootstrap mkfs.ext4 qemu-img; do
    if ! command -v $cmd &>/dev/null; then
        echo "Installing missing: $cmd..."
        apt-get update -qq && apt-get install -y -qq debootstrap e2fsprogs qemu-utils
        break
    fi
done

log "=== VM Image Builder ==="
log "Host: $HOST_ARCH → Target: $TARGET_ARCH (qemu=$NEED_QEMU)"

# ── debootstrap ──
log "Phase 1: debootstrap Ubuntu 24.04..."
mkdir -p "$TARGET"
debootstrap --arch="$TARGET_ARCH" --components=main,universe \
    --include=ca-certificates,locales \
    noble "$TARGET" http://archive.ubuntu.com/ubuntu/

cat > "$TARGET/etc/apt/sources.list" <<'EOF'
deb http://archive.ubuntu.com/ubuntu noble main universe
deb http://archive.ubuntu.com/ubuntu noble-updates main universe
deb http://archive.ubuntu.com/ubuntu noble-security main universe
EOF

echo "en_US.UTF-8 UTF-8" > "$TARGET/etc/locale.gen"
chroot "$TARGET" locale-gen
chroot "$TARGET" update-locale LANG=en_US.UTF-8

# ── マウント ──
mount -t proc proc "$TARGET/proc"
mount -t sysfs sys "$TARGET/sys"
mount -o bind /dev "$TARGET/dev"

# ── パッケージ ──
log "Phase 2: Installing core + KDE..."
chroot "$TARGET" apt-get update
chroot "$TARGET" apt-get install -y --no-install-recommends \
    linux-image-generic initramfs-tools udev dbus network-manager \
    sudo curl wget
chroot "$TARGET" apt-get install -y --no-install-recommends \
    plasma-desktop plasma-workspace kwin-x11 sddm konsole \
    firefox xdotool wmctrl xauth dolphin
chroot "$TARGET" apt-get clean
rm -rf "$TARGET/var/lib/apt/lists/"* "$TARGET/usr/share/doc/"* \
       "$TARGET/usr/share/man/"* "$TARGET/var/cache/apt/archives/"*

# ── ユーザー + SDDM ──
log "Configuring user + autologin..."
chroot "$TARGET" useradd -m -s /bin/bash -G sudo agent
echo "agent:agent" | chroot "$TARGET" chpasswd
echo "agent ALL=(ALL) NOPASSWD:ALL" > "$TARGET/etc/sudoers.d/agent"
mkdir -p "$TARGET/etc/sddm.conf.d"
printf '[Autologin]\nUser=agent\nSession=plasma-x11\n' > "$TARGET/etc/sddm.conf.d/autologin.conf"

# ── systemd ──
ln -sf /lib/systemd/system/graphical.target "$TARGET/etc/systemd/system/default.target"
ln -sf /lib/systemd/system/sddm.service "$TARGET/etc/systemd/system/display-manager.service"
# SDDM を graphical.target で起動させる（display-manager.service.wants は誤り）
mkdir -p "$TARGET/etc/systemd/system/graphical.target.wants"
ln -sf /lib/systemd/system/sddm.service "$TARGET/etc/systemd/system/graphical.target.wants/sddm.service"
mkdir -p "$TARGET/etc/systemd/system/multi-user.target.wants"
ln -sf /lib/systemd/system/NetworkManager.service "$TARGET/etc/systemd/system/multi-user.target.wants/NetworkManager.service"

echo "/dev/vda / ext4 defaults 0 1" > "$TARGET/etc/fstab"
echo "ai-desktop" > "$TARGET/etc/hostname"

# ── カーネル抽出 ──
log "Extracting kernel + initrd..."
KERNEL_VER=$(ls "$TARGET/lib/modules" | head -1)
cp "$TARGET/boot/vmlinuz-$KERNEL_VER" "$OUTPUT_DIR/vmlinuz"
cp "$TARGET/boot/initrd.img-$KERNEL_VER" "$OUTPUT_DIR/initrd.img"
echo "console=tty0 console=ttyS0 root=/dev/vda rw" > "$OUTPUT_DIR/cmdline.txt"

# ── ext4 → qcow2 ──
log "Unmounting..."
for mp in /proc /sys /dev; do umount "$TARGET$mp" 2>/dev/null || true; done

ROOTFS_MB=$(du -sm --exclude="$TARGET/proc" --exclude="$TARGET/sys" --exclude="$TARGET/dev" "$TARGET" | cut -f1)
DISK_MB=$((ROOTFS_MB + ROOTFS_MB / 2))
log "Phase 3: Creating ext4 (${ROOTFS_MB}MB → ${DISK_MB}MB)..."
truncate -s "${DISK_MB}M" "$OUTPUT_DIR/disk.raw"
mkfs.ext4 -F -d "$TARGET" "$OUTPUT_DIR/disk.raw"

log "Phase 4: Converting to qcow2..."
qemu-img convert -f raw -O qcow2 "$OUTPUT_DIR/disk.raw" "$OUTPUT_DIR/$IMAGE_NAME"
rm -f "$OUTPUT_DIR/disk.raw"

log "=== Done ==="
ls -lh "$OUTPUT_DIR"/{desktop.qcow2,vmlinuz,initrd.img,cmdline.txt}
