#!/bin/bash
# Android Entrypoint — Android-x86 をQEMUで起動する
# カーネル直起動（/android/kernel + /android/initrd.img）を優先し、
# なければISOのgrubメニュー起動にフォールバックする
set -euo pipefail

ANDROID_ISO="${ANDROID_ISO:-/android/boot.iso}"
ANDROID_KERNEL="${ANDROID_KERNEL:-/android/kernel}"
ANDROID_INITRD="${ANDROID_INITRD:-/android/initrd.img}"
# Android-x86 は 4GB でカーネル直起動が停滞する実測あり（ISO検出後に
# 進行せずVNC真っ黒・adbd offline。2GBでは約2分でウィザード到達）。
# そのため既定は 2GB とする（エージェント用途に十分）。4GBが必要なら
# ANDROID_MEMORY=4096 を明示すること（起動しない場合は2GBに戻す）。
ANDROID_MEMORY="${ANDROID_MEMORY:-2048}"
ANDROID_CPUS="${ANDROID_CPUS:-4}"
# NICモデル（virtio-net既定。Android-x86でEthernet認識されない場合は
# e1000 を試すこと。`wifi_eth` 名ではframeworkが管理しない実測あり）
ANDROID_NIC="${ANDROID_NIC:-virtio-net}"
# 永続dataイメージ（設定の永続化用）。なければ空作成する
# （初回は backend の機械的プロビジョニングが設定する）。
# 動的払い出しでは共有破損を避けるため使わない（固定のみ）。
ANDROID_DATA_IMG="${ANDROID_DATA_IMG:-/android/data.img}"
ANDROID_DATA_SIZE_MB="${ANDROID_DATA_SIZE_MB:-1024}"
# APK持ち込み用ディレクトリ（./scripts/fetch-apks.sh で取得）。
# *.apk があればFATイメージ化してUSBストレージとして接続する。
ANDROID_APKS_DIR="${ANDROID_APKS_DIR:-/android/apks}"
# ADB転送（backendからのスリープ無効化の強制経路用）。
# ゲストのTCP :5555 をコンテナの $ANDROID_ADB_PORT に転送する。
# backendは到達時のみADB強制、通常はVNC経路で設定する。既定ON（強制の要）。
# 無効化は ANDROID_ADB_FORWARD=0。
# hostfwdは 0.0.0.0 bind必須。127.0.0.1ではコンテナloのみlistenし、
# backend別コンテナ（android:5555 / and-*:5555）やホストpublishから届かない。
ANDROID_ADB_FORWARD="${ANDROID_ADB_FORWARD:-1}"
ANDROID_ADB_PORT="${ANDROID_ADB_PORT:-5555}"
# ADB over TCP（同梱なし/VNCなし用）。initrdパッチと対になる。
# ゲストinitが default.prop に service.adb.tcp.port を追記してadbdをTCP listenさせる。
ANDROID_ADB_TCP="${ANDROID_ADB_TCP:-1}"
# VirtWifi無効化（既定0=Ethernet）。VIRT_WIFI未指定時はvirt_wifiがeth0をwifi_ethに
# 改名しwlan0を作るが、9.0のEthernet DHCPはeth0のみ管理するためwifi_ethはIPv4不通となり、
# ADB offline停滞の原因になる。0ではeth0のまま残り、EthernetTrackerがDHCPで10.0.2.15を
# 自動取得し、ADB online・ネット疎通する（VNC操作・seed不要）。
# VirtWifiが必要な場合のみ1にする（その場合はUI接続が必要なため対象外）。
ANDROID_VIRT_WIFI="${ANDROID_VIRT_WIFI:-0}"
VNC_DISPLAY=$((ANDROID_VNC_PORT - 5900))
USE_KVM="${USE_KVM:-auto}"
ALLOW_TCG_VM="${ALLOW_TCG_VM:-false}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

if [ ! -f "$ANDROID_ISO" ]; then
    log "ERROR: Android ISO がありません: $ANDROID_ISO"
    log "ERROR: android/README.md の手順でISOを取得してください"
    exit 1
fi

# ── QEMU 起動オプション（vm/entrypoint.sh と同じ方針） ──

KVM_AVAILABLE=false
if [ -e /dev/kvm ]; then
    KVM_AVAILABLE=true
fi

WANT_KVM="$USE_KVM"
if [ "$WANT_KVM" = "auto" ]; then
    if [ "$KVM_AVAILABLE" = "true" ]; then
        WANT_KVM="true"
    else
        WANT_KVM="false"
    fi
fi

QEMU_ACCEL=()
if [ "$WANT_KVM" = "true" ]; then
    if [ "$KVM_AVAILABLE" != "true" ]; then
        log "ERROR: KVM が要求されましたが /dev/kvm がありません"
        exit 1
    fi
    log "KVM acceleration enabled"
    QEMU_ACCEL=(-enable-kvm -cpu host -smp "$ANDROID_CPUS")
else
    if [ "$ALLOW_TCG_VM" != "true" ]; then
        log "ERROR: KVM が利用できないためAndroid起動を制限します（TCGは遅すぎます）。"
        log "ERROR: デバッグ目的で起動する場合は ALLOW_TCG_VM=true を設定してください。"
        exit 1
    fi
    log "Using TCG software emulation (no KVM, explicitly allowed)"
    QEMU_ACCEL=(-cpu qemu64 -smp 1)
fi

log "Starting Android-x86 (VNC:0.0.0.0:$ANDROID_VNC_PORT, RAM:${ANDROID_MEMORY}MB, CPUs:${ANDROID_CPUS}, KVM=$WANT_KVM)"

# ── ネットワーク: user-mode NAT＋ADB転送 ──
# backendのADB強制経路が backend→<container>:$ANDROID_ADB_PORT で届くよう、
# ゲスト :5555 へ転送する。ゲストadbd不在でもQEMU自体は起動する。
NETDEV_OPTS=(-device "$ANDROID_NIC",netdev=net0 -netdev "user,id=net0")
if [ "$ANDROID_ADB_FORWARD" = "1" ] && [ -n "$ANDROID_ADB_PORT" ]; then
    log "ADB forward: container :$ANDROID_ADB_PORT -> guest :5555"
    NETDEV_OPTS=(
        -device "$ANDROID_NIC",netdev=net0
        -netdev "user,id=net0,hostfwd=tcp:0.0.0.0:$ANDROID_ADB_PORT-:5555"
    )
fi

BOOT_ARGS=()
DATA_DRIVE=()
USB_DRIVE=()
if [ -f "$ANDROID_KERNEL" ] && [ -f "$ANDROID_INITRD" ]; then
    # カーネル直起動: grubメニューを経由せずLive相当で起動する（約2分でGUI）
    # -append 既定は isolinux.cfg の `nosetup` エントリと同一
    # （`root=/dev/ram0 quiet SRC= DATA= SETUPWIZARD=0`）。
    # SETUPWIZARD=0 はセットアップウィザードを無人スキップし、
    # ホーム画面から起動する（ro.setupwizard.mode=DISABLED）。
    # 全文は ANDROID_APPEND で上書き可（起動診断用）。
    # androidboot.hardware=android_x86 を付与すると起動ロゴで停止するため付けないこと。
    log "Direct kernel boot: $ANDROID_KERNEL"
    # data.img の実体決定。ホスト側ファイル不在でvolumeが空ディレクトリ化
    # していた場合（マウントポイントはrmdir不可）はコンテナ内に作る。
    # いずれもゲストからは /dev/vda（DATA=vda）として見える。
    DATA_FILE="$ANDROID_DATA_IMG"
    if [ -d "$DATA_FILE" ]; then
        log "WARN: $DATA_FILE is a directory (empty volume). using container-local file."
        DATA_FILE=/android/data-local.img
    fi
    if [ ! -f "$DATA_FILE" ]; then
        log "Creating empty data disk ($ANDROID_DATA_SIZE_MB MB)"
        truncate -s "${ANDROID_DATA_SIZE_MB}M" "$DATA_FILE"
        mkfs.ext4 -q -F "$DATA_FILE"
    fi
    # ADB_TCP=1 を -append で渡す。initrdパッチが拾って default.prop に
    # service.adb.tcp.port=5555 / ro.adb.secure=0 を追記し、同梱なし/VNCなしで
    # ゲストadbdをTCP listenさせる。全文上書き(ANDROID_APPEND)時は呼び出し側で含めること。
    APPEND_DEFAULT="root=/dev/ram0 quiet SRC= DATA= SETUPWIZARD=0 ADB_TCP=$ANDROID_ADB_TCP VIRT_WIFI=$ANDROID_VIRT_WIFI"
    if [ -f "$DATA_FILE" ]; then
        log "Persistent data disk: $DATA_FILE (DATA=vda)"
        APPEND_DEFAULT="root=/dev/ram0 quiet SRC= DATA=vda SETUPWIZARD=0 ADB_TCP=$ANDROID_ADB_TCP VIRT_WIFI=$ANDROID_VIRT_WIFI"
        DATA_DRIVE=(-drive "file=$DATA_FILE,if=virtio,format=raw")
    fi
    BOOT_ARGS=(
        -kernel "$ANDROID_KERNEL"
        -initrd "$ANDROID_INITRD"
        -append "${ANDROID_APPEND:-$APPEND_DEFAULT}"
    )
else
    log "Kernel files not found, falling back to ISO boot menu (grub経由・約2分)"
fi

# ── APK持ち込み: *.apk をFATイメージ化してUSBストレージで渡す ──
# ゲストにはUSBドライブとして見える。ファイルアプリから開いて導入する。
# ネット不要・ゲスト無改造でAPKを配備できる。
if compgen -G "$ANDROID_APKS_DIR/*.apk" > /dev/null; then
    # mkfs.vfat -C は既存ファイルへの上書きを拒むため一旦消す
    APKS_IMG="$(mktemp /tmp/apks-XXXXXX.img)" && rm -f "$APKS_IMG"
    APKS_KB="$(du -sk "$ANDROID_APKS_DIR" | cut -f1)"
    mkfs.vfat -C "$APKS_IMG" $((APKS_KB + 2048))
    for apk in "$ANDROID_APKS_DIR"/*.apk; do
        mcopy -i "$APKS_IMG" "$apk" ::
    done
    log "USB apks attached from $ANDROID_APKS_DIR"
    USB_DRIVE=(
        -drive "file=$APKS_IMG,if=none,id=usbdisk,format=raw"
        -device "usb-storage,drive=usbdisk,removable=true"
    )
fi

exec qemu-system-x86_64 \
    "${QEMU_ACCEL[@]}" \
    -m "$ANDROID_MEMORY" \
    "${BOOT_ARGS[@]}" \
    "${DATA_DRIVE[@]}" \
    "${USB_DRIVE[@]}" \
    -cdrom "$ANDROID_ISO" \
    -boot order=d \
    -vnc "0.0.0.0:$VNC_DISPLAY" \
    "${NETDEV_OPTS[@]}" \
    -serial none \
    -display none \
    -usb \
    -device usb-tablet
