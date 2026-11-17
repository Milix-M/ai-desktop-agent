#!/bin/bash
# build-vm-image.sh — ホスト上で debootstrap → qcow2 を構築
# x86_64 / ARM64 両対応
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="${VM_OUTPUT_DIR:-$SCRIPT_DIR}"
IMAGE_NAME="desktop.qcow2"
WORK_DIR=""
TARGET=""
STAGING=""
PUBLISHED=()

log() { echo "[$(date '+%H:%M:%S')] $*"; }

unmount_rootfs() {
    local mp status failed=false
    for mp in /dev /sys /proc; do
        if mountpoint -q -- "$TARGET$mp"; then
            if ! umount -- "$TARGET$mp"; then
                log "ERROR: cannot unmount $TARGET$mp" >&2
                failed=true
            fi
        else
            status=$?
            if [ "$status" -ne 32 ]; then
                log "ERROR: cannot check mount state: $TARGET$mp" >&2
                failed=true
            fi
        fi
    done
    [ "$failed" = false ]
}

rollback_publication() {
    local name failed=false backup
    for name in "${PUBLISHED[@]}"; do
        backup="$WORK_DIR/previous/$name"
        if [ -e "$backup" ] || [ -L "$backup" ]; then
            if [ ! "$backup" -ef "$OUTPUT_DIR/$name" ]; then
                mv -f -- "$backup" "$OUTPUT_DIR/$name" || failed=true
            fi
        else
            rm -f -- "$OUTPUT_DIR/$name" || failed=true
        fi
    done
    [ "$failed" = false ]
}

cleanup() {
    local status=$? preserve=false
    trap - EXIT
    # A failed cleanup must not prevent the remaining checks from running.
    set +e
    if [ -z "$WORK_DIR" ] || [ "$TARGET" != "$WORK_DIR/rootfs" ]; then
        log "ERROR: invalid build directory; refusing cleanup" >&2
        exit 1
    fi
    if ! rollback_publication; then
        log "ERROR: restoring previous guest files failed" >&2
        preserve=true
    fi
    if ! unmount_rootfs; then
        preserve=true
    fi
    if [ "$preserve" = true ]; then
        log "ERROR: preserving build files for recovery: $WORK_DIR" >&2
        [ "$status" -ne 0 ] || status=1
    elif ! rm -rf --one-file-system -- "$WORK_DIR"; then
        log "ERROR: cannot remove build files: $WORK_DIR" >&2
        [ "$status" -ne 0 ] || status=1
    fi
    exit "$status"
}

publish_artifacts() {
    local name
    mkdir -p "$WORK_DIR/previous"
    for name in "$IMAGE_NAME" vmlinuz initrd.img cmdline.txt; do
        if [ -e "$OUTPUT_DIR/$name" ] || [ -L "$OUTPUT_DIR/$name" ]; then
            # Work and output directories share a filesystem; backups need no disk copy.
            cp -a --link -- "$OUTPUT_DIR/$name" "$WORK_DIR/previous/$name"
        fi
    done
    for name in "$IMAGE_NAME" vmlinuz initrd.img cmdline.txt; do
        # Record intent first so a signal between rename and the next command rolls back.
        PUBLISHED+=("$name")
        mv -f -- "$STAGING/$name" "$OUTPUT_DIR/$name"
    done
    PUBLISHED=()
}

main() {

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
    for cmd in debootstrap mkfs.ext4 qemu-img mountpoint flock; do
        if ! command -v $cmd &>/dev/null; then
            echo "Installing missing: $cmd..."
            apt-get update -qq && apt-get install -y -qq debootstrap e2fsprogs qemu-utils util-linux
            break
        fi
    done

    mkdir -p "$OUTPUT_DIR"
    exec {BUILD_LOCK}>"$OUTPUT_DIR/.vm-image-build.lock"
    if ! flock -n "$BUILD_LOCK"; then
        log "ERROR: another VM image build is using $OUTPUT_DIR" >&2
        return 1
    fi
    for file in "$IMAGE_NAME" vmlinuz initrd.img cmdline.txt; do
        if [ -d "$OUTPUT_DIR/$file" ] || { [ -e "$OUTPUT_DIR/$file" ] && [ ! -f "$OUTPUT_DIR/$file" ]; }; then
            log "ERROR: guest output is not a regular file: $OUTPUT_DIR/$file" >&2
            return 1
        fi
    done
    WORK_DIR=$(mktemp -d "$OUTPUT_DIR/.vm-build.XXXXXXXX")
    TARGET="$WORK_DIR/rootfs"
    STAGING="$WORK_DIR/output"
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    mkdir -p "$STAGING"

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
    mount -t sysfs -o ro sys "$TARGET/sys"
    mount -o bind /dev "$TARGET/dev"

    # ── パッケージ ──
    log "Phase 2: Installing core + KDE..."
    chroot "$TARGET" apt-get update
    chroot "$TARGET" apt-get install -y --no-install-recommends \
        linux-image-generic initramfs-tools udev dbus network-manager \
        sudo curl wget
    chroot "$TARGET" apt-get install -y --no-install-recommends \
        plasma-desktop plasma-workspace kwin-x11 sddm konsole \
        firefox xdotool wmctrl xauth dolphin \
        qemu-guest-agent
    # guest agent は初回起動時に systemd が自動起動する。
    # QEMU 側の virtio-serial チャネルは entrypoint.sh で付与する。
    # 注意: Ubuntu 24.04 の firefox パッケージは snap 移行用のスタブ。
    # chroot 内では snapd が動かないため実体が入らない。初回起動後に
    # ゲスト内で `sudo snap install firefox` を実行すること。
    # （将来的には Mozilla Team PPA からの firefox-esr 導入を検討）
    chroot "$TARGET" apt-get clean
    rm -rf "$TARGET/var/lib/apt/lists/"* "$TARGET/usr/share/doc/"* \
           "$TARGET/usr/share/man/"* "$TARGET/var/cache/apt/archives/"*

    # ── ユーザー + SDDM ──
    log "Configuring user + autologin..."
    chroot "$TARGET" useradd -m -s /bin/bash -G sudo agent
    echo "agent:agent" | chroot "$TARGET" chpasswd
    echo "agent ALL=(ALL) NOPASSWD:ALL" > "$TARGET/etc/sudoers.d/agent"
    # sudo 時の hostname 解決警告を抑止
    echo "127.0.1.1 ai-desktop" >> "$TARGET/etc/hosts"

    # スクリーンロック無効化（AIエージェント操作用）
    mkdir -p "$TARGET/home/agent/.config"
    cat > "$TARGET/home/agent/.config/kscreenlockerrc" <<'KSCREENLOCKER_EOF'
[Daemon]
Autolock=false
LockOnResume=false
KSCREENLOCKER_EOF

    cat > "$TARGET/home/agent/.config/powermanagementprofilesrc" <<'POWER_EOF'
[AC][Display]
TurnOffWhenIdle=false
[AC][SuspendAndShutdown]
SuspendWhenIdle=false
AutoSuspend=false
POWER_EOF

    chroot "$TARGET" chown -R agent:agent /home/agent/.config

    # ── 画面ブランク防止: DPMS/Xスクリーンセーバを無効化 ──
    # powerdevil の設定だけでは X の DPMS (600秒で消灯) が残るため、
    # ログイン時に xset で確実に切る。VNC が真っ黒になるのを防ぐ。
    mkdir -p "$TARGET/home/agent/.config/autostart"
    cat > "$TARGET/home/agent/.config/autostart/disable-dpms.desktop" <<'DPMS_EOF'
[Desktop Entry]
Type=Application
Name=Disable DPMS
Exec=sh -c 'xset s off; xset -dpms; xset s noblank'
X-GNOME-Autostart-enabled=true
DPMS_EOF
    chroot "$TARGET" chown -R agent:agent /home/agent/.config/autostart

    # ── ネットワーク: QEMU user-mode NAT (10.0.0.0/8) 前提で ens3 を DHCP 構成 ──
    # NetworkManager 単独では ens3 が unmanaged になるため netplan で明示する。
    # systemd-resolved の stub にも依存せず QEMU 内蔵DNS (10.0.2.3) を直接使う。
    mkdir -p "$TARGET/etc/netplan"
    cat > "$TARGET/etc/netplan/99-ens3.yaml" <<'NETPLAN_EOF'
network:
  version: 2
  renderer: NetworkManager
  ethernets:
    ens3:
      dhcp4: true
      dhcp6: false
NETPLAN_EOF
    chmod 600 "$TARGET/etc/netplan/99-ens3.yaml"
    rm -f "$TARGET/etc/resolv.conf"
    echo "nameserver 10.0.2.3" > "$TARGET/etc/resolv.conf"

    # SDDM 自動ログイン: 実際にインストールされたセッションを検出
    SESSION_NAME=""
    if [ -f "$TARGET/usr/share/xsessions/plasma.desktop" ]; then
        SESSION_NAME="plasma"
    elif [ -f "$TARGET/usr/share/xsessions/plasma-x11.desktop" ]; then
        SESSION_NAME="plasma-x11"
    elif [ -f "$TARGET/usr/share/xsessions/kde-plasma.desktop" ]; then
        SESSION_NAME="kde-plasma"
    else
        # フォールバック: 最初の .desktop を使用
        SESSION_NAME=$(ls "$TARGET/usr/share/xsessions/"*.desktop 2>/dev/null | head -1 | xargs basename | sed 's/\.desktop$//')
    fi
    log "Detected session: $SESSION_NAME"

    # /etc/sddm.conf に直接書き込み（.d/ より優先される）
    cat > "$TARGET/etc/sddm.conf" <<SDDM_EOF
[Autologin]
User=agent
Session=$SESSION_NAME

[Theme]
Current=breeze
SDDM_EOF

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
    cp "$TARGET/boot/vmlinuz-$KERNEL_VER" "$STAGING/vmlinuz"
    cp "$TARGET/boot/initrd.img-$KERNEL_VER" "$STAGING/initrd.img"
    echo "console=tty0 console=ttyS0 root=/dev/vda rw" > "$STAGING/cmdline.txt"
    mkdir -p "$TARGET/etc/ai-desktop-agent"
    cp "$STAGING/cmdline.txt" "$TARGET/etc/ai-desktop-agent/cmdline"

    # ── ext4 → qcow2 ──
    log "Unmounting..."
    unmount_rootfs

    ROOTFS_MB=$(du -sm --exclude="$TARGET/proc" --exclude="$TARGET/sys" --exclude="$TARGET/dev" "$TARGET" | cut -f1)
    DISK_MB=$((ROOTFS_MB + ROOTFS_MB / 2))
    # 最低 15GB を確保（KDE + snap アプリ導入の余裕。3〜4GB では即枯渇する）
    if [ "$DISK_MB" -lt 15360 ]; then
        DISK_MB=15360
    fi
    log "Phase 3: Creating ext4 (${ROOTFS_MB}MB → ${DISK_MB}MB)..."
    truncate -s "${DISK_MB}M" "$STAGING/disk.raw"
    mkfs.ext4 -F -d "$TARGET" "$STAGING/disk.raw"

    log "Phase 4: Converting to qcow2..."
    qemu-img convert -f raw -O qcow2 "$STAGING/disk.raw" "$STAGING/$IMAGE_NAME"
    rm -f "$STAGING/disk.raw"
    publish_artifacts

    log "=== Done ==="
    ls -lh "$OUTPUT_DIR"/{desktop.qcow2,vmlinuz,initrd.img,cmdline.txt}
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
