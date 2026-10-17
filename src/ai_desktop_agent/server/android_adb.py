"""Android初期設定のADB経路。

seed同梱なしで空data.imgから起動し、ADBのshell命令で初期設定する。
VNCタップ列は使わない。ゲストadbdのTCP listenは initrdパッチ
(scripts/patch-android-initrd.py + entrypointのADB_TCP=1)が
default.propに service.adb.tcp.port=5555 / ro.adb.secure=0 を追記して開ける。
ネットワークは VIRT_WIFI=0 (既定) でeth0のまま残し、EthernetTrackerが
DHCPで10.0.2.15を自動取得する。virt_wifi有効時はeth0がwifi_ethに改名され
Ethernet管理外となりIPv4不通・ADB offline停滞の実測あり。
到達不能時はNoneやFalseを返し、呼び出し側は警告記録に留める。

設定する項目は再起動を超えて/dataに残るものとする。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess

logger = logging.getLogger(__name__)

#: Sleep無効化相当のタイムアウトでAndroid上限のint32 maxとする。
SCREEN_OFF_TIMEOUT_MAX_MS = 2147483647
#: 充電中は寝かせないものとし、bitmaskはAC=1とUSB=2とWIRELESS=4の全電源とする。
STAY_ON_WHILE_PLUGGED_IN = 7
#: 既定タイムゾーンはホストJSTと一致するAsia/Tokyoとする。
DEFAULT_TIMEZONE = "Asia/Tokyo"
#: 既定の接続先Wi-FiはVirtWifiとする。
DEFAULT_WIFI_SSID = "VirtWifi"
#: ADB接続の既定タイムアウトは秒とし、起動直後の待機で長引かせない。
ADB_TIMEOUT = 10.0


def _adb_bin() -> str | None:
    """adb実行ファイルのパスとする。なければNoneとし、VNCにも触れない。"""
    return shutil.which("adb")


def _run_adb(serial: str, *args: str, timeout: float = ADB_TIMEOUT) -> bool:
    """1つのadb命令を実行し、成功時Trueとする。"""
    adb = _adb_bin()
    if adb is None:
        return False
    try:
        proc = subprocess.run(
            [adb, "-s", serial, *args],
            capture_output=True,
            timeout=timeout,
            check=False,
            text=True,
        )
        ok = proc.returncode == 0
        if not ok:
            logger.debug("adb失敗: %s %s: %s", serial, args, proc.stderr[:200])
        return ok
    except (OSError, subprocess.SubprocessError):
        logger.debug("adb実行に失敗: %s %s", serial, args, exc_info=True)
        return False


def _run_adb_out(serial: str, *args: str, timeout: float = ADB_TIMEOUT) -> str | None:
    """adb命令の標準出力を返すものとし、失敗時はNoneとする。"""
    adb = _adb_bin()
    if adb is None:
        return None
    try:
        proc = subprocess.run(
            [adb, "-s", serial, *args],
            capture_output=True,
            timeout=timeout,
            check=False,
            text=True,
        )
        if proc.returncode != 0:
            logger.debug("adb照会失敗: %s %s: %s", serial, args, proc.stderr[:200])
            return None
        return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        logger.debug("adb照会に失敗: %s %s", serial, args, exc_info=True)
        return None


def _run_connect(serial: str, timeout: float = ADB_TIMEOUT) -> bool:
    """`adb connect <serial>` を実行し、出力で成否判定する。

    `adb connect` は到達不能でも rc=0 を返す実測があるため、rc判定は不可。
    (`Connection refused`でもrc=0)。stdout/stderrに `connected to` を含むかで判定する。
    `-s` は付けない（未接続時に `-s` 選別が先に失敗する）。
    """
    adb = _adb_bin()
    if adb is None:
        return False
    try:
        proc = subprocess.run(
            [adb, "connect", serial],
            capture_output=True,
            timeout=timeout,
            check=False,
            text=True,
        )
        out = f"{proc.stdout}\n{proc.stderr}".lower()
        return "connected to" in out or "already connected" in out
    except (OSError, subprocess.SubprocessError):
        logger.debug("adb connect実行に失敗: %s", serial, exc_info=True)
        return False


def connect(host: str, port: int = 5555, runner=None) -> str | None:
    """ADB接続し、成功時はシリアルを返し、失敗時はNoneとする。

    Args:
        host: 接続先ホストとし、固定androidはサービス名、動的はコンテナ名とする。
        port: ADBポートとし、QEMU hostfwdの転送先とする。
        runner: テスト用の差し替えとし、``(serial, *args) -> bool``とする。
            指定時は旧来のbool判定（テスト用）。未指定時は実機向けに出力判定する。
    """
    serial = f"{host}:{port}"
    if runner is not None:
        try:
            if runner(serial, "connect", serial):
                return serial
        except Exception:
            logger.debug("adb connectに失敗", exc_info=True)
        return None
    try:
        if _run_connect(serial):
            return serial
    except Exception:
        logger.debug("adb connectに失敗", exc_info=True)
    return None


def setup_sleep(serial: str, runner=None) -> bool:
    """スリープしない設定を入れ、成功時Trueとする。"""
    run = runner or _run_adb
    ok = True
    for c in (
        ("shell", "settings", "put", "system", "screen_off_timeout",
         str(SCREEN_OFF_TIMEOUT_MAX_MS)),
        ("shell", "settings", "put", "global", "stay_on_while_plugged_in",
         str(STAY_ON_WHILE_PLUGGED_IN)),
        ("shell", "svc", "power", "stayon", "true"),
        ("shell", "settings", "put", "secure", "screensaver_enabled", "0"),
        ("shell", "settings", "put", "secure", "screensaver_activate_on_sleep", "0"),
        ("shell", "settings", "put", "secure", "screensaver_activate_on_dock", "0"),
    ):
        try:
            if not run(serial, *c):
                ok = False
        except Exception:
            logger.debug("adb設定に失敗: %s", c, exc_info=True)
            ok = False
    if ok:
        logger.info("ADBでスリープしない設定: %s", serial)
    return ok


def setup_timezone(serial: str, timezone: str = DEFAULT_TIMEZONE, runner=None) -> bool:
    """タイムゾーンを設定し、成功時Trueとする。"""
    run = runner or _run_adb
    try:
        return bool(run(serial, "shell", "setprop", "persist.sys.timezone", timezone))
    except Exception:
        logger.debug("adb timezone設定に失敗", exc_info=True)
        return False


def setup_wifi(serial: str, ssid: str = DEFAULT_WIFI_SSID, runner=None) -> bool:
    """Wi-Fiを有効化して open のSSIDに接続し、成功時Trueとする。

    9.0-r2の `cmd wifi` はshell権限(Uid 2000)ではSecurityExceptionで使えず、
    VIRT_WIFI=0既定ではwlan0自体が存在しないため失敗する。その場合は
    Ethernet(eth0)の疎通があれば呼び出し側で成功扱いにすること。
    """
    run = runner or _run_adb
    try:
        if not run(serial, "shell", "svc", "wifi", "enable"):
            return False
        return bool(run(serial, "shell", "cmd", "wifi", "connect-network", ssid, "open"))
    except Exception:
        logger.debug("adb wifi設定に失敗", exc_info=True)
        return False


def has_network(serial: str, query=None) -> bool:
    """ゲストにIPv4疎通があるか。eth0/wlan0のinet有無で判定する。

    VIRT_WIFI=0既定ではeth0がDHCPで10.0.2.15を取得する。wlan0のみの
    旧構成ではVirtWifi接続が必要だが、9.0の `cmd wifi` はshell権限で使えない
    ため、Ethernet疎通をもって成功とみなす。
    """
    out_of = query or _run_adb_out
    try:
        for iface in ("eth0", "wlan0", "wifi_eth"):
            out = out_of(serial, "shell", "ip", "addr", "show", iface)
            if out and "inet " in out and "127.0.0.1" not in out.replace("inet 127.0.0.1", ""):
                # 127以外も探すため、inet行に10./192.168/172.16等があればTrue
                for line in out.splitlines():
                    line = line.strip()
                    if line.startswith("inet ") and not line.startswith("inet 127."):
                        return True
        return False
    except Exception:
        logger.debug("adb network確認に失敗", exc_info=True)
        return False


def _pick_home_component(output: str) -> str | None:
    """HOME候補の出力から部品名を1つ選ぶものとし、なければNoneとする。"""
    cands = re.findall(r"[A-Za-z][\w.]*\/[\w.$]+", output or "")
    if not cands:
        return None
    for key in ("quickstep", "launcher", "taskbar"):
        for c in cands:
            if key in c.lower():
                return c
    return cands[0]


def setup_home(serial: str, runner=None, query=None) -> bool:
    """HOME選択をQuickstep等に固定し、成功時Trueとする。"""
    run = runner or _run_adb
    out_of = query or _run_adb_out
    try:
        # 全HOME候補を列挙する（resolve-activityは選択中ダイアログ自体を返すため不可）。
        # pm query-activities --components は pkg/activity 行を返す。
        out = out_of(
            serial,
            "shell",
            "pm",
            "query-activities",
            "--components",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.HOME",
        )
        if not out:
            out = out_of(
                serial,
                "shell",
                "cmd",
                "package",
                "resolve-activity",
                "--brief",
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.HOME",
            )
    except Exception:
        logger.debug("adb HOME候補照会に失敗", exc_info=True)
        return False
    comp = _pick_home_component(out or "")
    if comp is None:
        logger.warning("HOME候補が見つからない: %s", serial)
        return False
    try:
        # 9.0-r2世代は `cmd package set-home-activity` が無い場合があるため、
        # 失敗時は旧来の `pm set-home-activity` にフォールバックする。
        # いずれもseed/VNCなしのADBのみで完結する。
        if run(serial, "shell", "cmd", "package", "set-home-activity", comp):
            ok = True
        else:
            ok = bool(run(serial, "shell", "pm", "set-home-activity", comp))
    except Exception:
        logger.debug("adb HOME固定に失敗", exc_info=True)
        return False
    if ok:
        logger.info("ADBでHOME固定: %s %s", serial, comp)
    return ok


def dismiss_dialogs(serial: str, runner=None) -> bool:
    """選択ダイアログ残留をHOMEキーで消す(ADBのみ、VNC不使用)。

    初回起動ではHome確定後もダイアログが残る実測があるため、
    HOMEキーを送ってランチャー直行させる。失敗しても無害(ベストエフォート)。
    """
    run = runner or _run_adb
    try:
        # HOME→BACKの順でダイアログを閉じてホームへ
        run(serial, "shell", "input", "keyevent", "3")
        return True
    except Exception:
        logger.debug("adb dialog-dismissに失敗", exc_info=True)
        return False


def install_apk(serial: str, apk_path: str, query=None, timeout: float = 120.0) -> bool:
    """APKを導入し、成功時Trueとする。導入済みも成功扱いとする。

    初回起動直後の12MB級APKは10秒を超える実測があるため既定120秒とする。
    """
    from functools import partial as _partial

    if query is not None:
        out_of = query
    else:
        # _run_adb_outにtimeoutを渡す
        import functools as _functools

        out_of = _functools.partial(_run_adb_out, timeout=timeout)
    try:
        out = out_of(serial, "install", "-r", apk_path)
    except Exception:
        logger.debug("adb installに失敗: %s", apk_path, exc_info=True)
        return False
    if out is None:
        return False
    if "Success" in out or "ALREADY_EXISTS" in out:
        logger.info("ADBでAPK導入: %s %s", serial, apk_path)
        return True
    logger.warning("ADBでAPK導入に失敗: %s %s", serial, out[:200])
    return False
