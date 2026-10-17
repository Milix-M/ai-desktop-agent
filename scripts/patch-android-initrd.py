#!/usr/bin/env python3
"""android/initrd.img に ADB-over-TCP＋Home既定をパッチする。

seed同梱なし・VNCタップ列なしで backend から到達させるための boot 側修正。
- kernel cmdline の ADB_TCP=1 (entrypoint の -append) を init が拾い、
  ramdisk 由来の default.prop に service.adb.tcp.port / ro.adb.secure を追記する。
- 空data初回の Home選択ダイアログを rcフックで自動確定する。
- data.img の事前seedはしない。証跡のみ data/ に残す（診断用）。
  ゲストUI操作はしない。

使い方: python3 scripts/patch-android-initrd.py [initrdパス]
idempotent（2回目以降は何もしない）。
"""
from __future__ import annotations

import gzip
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INITRD = REPO_ROOT / "android" / "initrd.img"

MARKER = "ADB_TCP"
HOME_MARKER = "set-home-activity"
EVIDENCE_MARKER = "initrd-patch-default.prop"
PATCH_BLOCK = """# ADB over TCP (同梱なし/VNCなしでbackend到達用。DATA不問)
# entrypoint の -append ADB_TCP=1 を拾い、ramdisk の default.prop に追記する。
# service.adb.tcp.port=5555 でゲストadbdをTCP listenさせ、
# ro.adb.secure=0 で初回RSA承認ダイアログを出さない（隔離net内のみ）。
if [ "${ADB_TCP:-1}" != "0" ]; then
  echo "service.adb.tcp.port=5555" >> default.prop
  echo "ro.adb.secure=0" >> default.prop
fi
# Home既定 (同梱なし/VNCなし用)。空data初回はQuickstep/Taskbar選択が出て
# boot完了・ADB onlineを止める。sys.boot_completed待ちでは遅い（選択中は来ない実測）
# ため、boot時にserviceを上げてPM準備完了までリトライする。
# provisioner の setup_home と冪等。VNCタップは使わない。
# initのservice/execは引用符を解釈しないため、複文はscriptファイル化する。
cat > data/home-setup.sh <<'EOF_SH' 2>/dev/null || true
#!/system/bin/sh
{
echo "start"
id
getprop sys.boot_completed
echo "=== wifi help ==="
cmd wifi help 2>&1 | head -60
echo "=== service list net ==="
service list 2>&1 | grep -i -E "wifi|netd|connect" | head -20
echo "=== wpa_cli ==="
which wpa_cli 2>&1
wpa_cli -i wlan0 status 2>&1 | head -20
ls /data/misc/wifi/ 2>&1 | head -20
echo "=== dumpsys wifi ==="
dumpsys wifi 2>&1 | head -80
echo "=== ip rule ==="
ip rule show 2>&1 | head -20
echo "=== route all ==="
ip route show table all 2>&1 | head -40
echo "=== dhcp bins ==="
ls /system/bin/dhcp* 2>&1 | head
which dhcpcd netcfg ndc 2>&1
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
  echo "try $i"
  if cmd package set-home-activity --user 0 com.android.launcher3/com.android.launcher3.Launcher 2>&1; then
    echo home:ok > /data/home-hook.log
    getprop sys.boot_completed >> /data/home-hook.log
    echo "=== netfix11 ===" >> /data/home-setup-err.log
    echo "--- dhcpcd try ---" >> /data/home-setup-err.log
    dhcpcd -B wifi_eth >> /data/home-setup-err.log 2>&1
    echo "dhcpcd_rc=$?" >> /data/home-setup-err.log
    sleep 5
    ip addr show wifi_eth >> /data/home-setup-err.log 2>&1
    ip addr show wlan0 >> /data/home-setup-err.log 2>&1
    echo "--- netcfg try ---" >> /data/home-setup-err.log
    netcfg wifi_eth dhcp >> /data/home-setup-err.log 2>&1
    echo "netcfg_rc=$?" >> /data/home-setup-err.log
    sleep 5
    ip addr show wifi_eth >> /data/home-setup-err.log 2>&1
    echo "--- wifi 9.0 try ---" >> /data/home-setup-err.log
    svc wifi enable >> /data/home-setup-err.log 2>&1
    sleep 5
    cmd wifi list-networks >> /data/home-setup-err.log 2>&1
    cmd wifi add-network VirtWifi open >> /data/home-setup-err.log 2>&1
    sleep 2
    cmd wifi list-networks >> /data/home-setup-err.log 2>&1
    echo "--- wpa try ---" >> /data/home-setup-err.log
    wpa_cli -i wlan0 list_networks >> /data/home-setup-err.log 2>&1
    wpa_cli -i wlan0 add_network >> /data/home-setup-err.log 2>&1
    ip route show table all >> /data/home-setup-err.log 2>&1
    ping -c1 -W5 10.0.2.2 >> /data/home-setup-err.log 2>&1
    echo "ping_rc=$?" >> /data/home-setup-err.log
    echo home:netfix11-done >> /data/home-hook.log
    exit 0
  fi
  sleep 10
done
echo home:ng > /data/home-hook.log
} > /data/home-setup-err.log 2>&1
EOF_SH
chmod 755 data/home-setup.sh 2>/dev/null || true
cp -f data/home-setup.sh data/initrd-patch-home-sh.log 2>/dev/null || true
if ! grep -q "home_setup" init.android_x86_64.rc 2>/dev/null; then
  cat >> init.android_x86_64.rc <<'EOF_HOME'
service home_setup /system/bin/sh /data/home-setup.sh
    class main
    user root
    group root
    disabled
    oneshot
on boot
    start home_setup
EOF_HOME
fi
# 証跡 (診断用。seedではなく適用確認のみ)。data/はmount_data済みのdata.img実体。
cp -f default.prop data/initrd-patch-default.prop 2>/dev/null || true
cp -f init.android_x86_64.rc data/initrd-patch-rc.log 2>/dev/null || true
echo "ADB_TCP=${ADB_TCP:-1} SETUPWIZARD=${SETUPWIZARD:-unset}" > data/initrd-patch-env.log 2>/dev/null || true
"""

ANCHOR = '[ "$SETUPWIZARD" = "0" ] && echo "ro.setupwizard.mode=DISABLED" >> default.prop'


def _parse_cpio(data: bytes) -> list[tuple[dict, bytes]]:
    """newc エントリを (header-fields, content) で返す。TRAILER含む。"""
    entries = []
    off = 0
    while off + 110 <= len(data):
        magic = data[off : off + 6]
        if magic not in (b"070701", b"070702"):
            raise ValueError(f"bad cpio magic at {off}: {magic!r}")
        fields = [int(data[off + 6 + i * 8 : off + 6 + (i + 1) * 8], 16) for i in range(13)]
        namesize = fields[11]
        filesize = fields[6]
        name = data[off + 110 : off + 110 + namesize - 1].decode()
        header_len = 110 + namesize
        pad = (4 - (header_len % 4)) % 4
        data_off = off + header_len + pad
        content = data[data_off : data_off + filesize] if filesize else b""
        entries.append(
            (
                {
                    "ino": fields[0],
                    "mode": fields[1],
                    "uid": fields[2],
                    "gid": fields[3],
                    "nlink": fields[4],
                    "mtime": fields[5],
                    "filesize": filesize,
                    "maj": fields[7],
                    "min": fields[8],
                    "rmaj": fields[9],
                    "rmin": fields[10],
                    "namesize": namesize,
                    "check": fields[12],
                    "name": name,
                    "magic": magic,
                },
                content,
            )
        )
        if name == "TRAILER!!!":
            break
        off = data_off + filesize
        off += (4 - (filesize % 4)) % 4 if filesize else 0
    return entries


def _encode_entry(meta: dict, content: bytes) -> bytes:
    name_b = meta["name"].encode() + b"\x00"
    namesize = len(name_b)
    header = meta["magic"]
    for v in (
        meta["ino"],
        meta["mode"],
        meta["uid"],
        meta["gid"],
        meta["nlink"],
        meta["mtime"],
        len(content),
        meta["maj"],
        meta["min"],
        meta["rmaj"],
        meta["rmin"],
        namesize,
        meta["check"],
    ):
        header += f"{v:08x}".encode()
    header += name_b
    pad = (4 - ((110 + namesize) % 4)) % 4
    header += b"\x00" * pad
    out = header + content
    out += b"\x00" * ((4 - (len(content) % 4)) % 4) if content else b""
    return out


def main() -> int:
    initrd = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INITRD
    if not initrd.is_file():
        print(f"initrdがありません: {initrd}", file=sys.stderr)
        return 1
    raw = gzip.open(initrd, "rb").read()
    entries = _parse_cpio(raw)
    patched = False
    out = b""
    for meta, content in entries:
        if meta["name"] == "init":
            text = content.decode()
            if ANCHOR not in text:
                print("ANCHORが見つからない、init形式が変わっている", file=sys.stderr)
                return 1
            # 旧パッチあり→最新へ置換する（v1/v2/v3/v4混在対応）。
            # 最新マーカー home-setup.sh があればskipする。
            if MARKER in text and "netfix11" in text and EVIDENCE_MARKER in text:
                print("already patched, skip")
                return 0
            if MARKER in text:
                start = text.find("# ADB over TCP")
                assert start != -1, "旧ブロック開始が見つからない"
                # 旧ブロック終端: 証跡行（v3）か HOME fi（v1/v2）のいずれか
                end_markers = [
                    'echo "ADB_TCP=${ADB_TCP:-1} SETUPWIZARD=${SETUPWIZARD:-unset}" > data/initrd-patch-env.log 2>/dev/null || true\n',
                    "EOF_HOME\nfi\n",
                ]
                end = -1
                for m in end_markers:
                    idx = text.find(m, start)
                    if idx != -1:
                        end = idx + len(m)
                        break
                # v1（HOMEなし・証跡なし）は旧ADB fiまで
                if end == -1:
                    m1 = '  echo "ro.adb.secure=0" >> default.prop\nfi\n'
                    idx = text.find(m1, start)
                    assert idx != -1, "旧ブロック終端が見つからない"
                    end = idx + len(m1)
                text = text[:start] + PATCH_BLOCK + text[end:]
                text = text.replace("seedなし/VNCなし", "同梱なし/VNCなし")
                content = text.encode()
                patched = True
            elif MARKER not in text:
                text = text.replace(ANCHOR, ANCHOR + "\n" + PATCH_BLOCK, 1)
                content = text.encode()
                patched = True
        out += _encode_entry(meta, content)
    if not patched:
        print("init エントリが見つからない", file=sys.stderr)
        return 1
    initrd.write_bytes(gzip.compress(out, compresslevel=9, mtime=0))
    print(f"patched: {initrd}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
