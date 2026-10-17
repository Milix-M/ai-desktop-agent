"""Android初回設定のADB投入（LLM不使用）。

backend常駐任務が未設定の稼働端末にADBのshell命令で初期設定する。
VNCタップ列は使わない。seed同梱は不要とし、空data.imgから起動して
機械的操作のみで設定する。到達不能時は警告記録に留め、次回に再試行する。
ゲストadbdのTCP listenは initrdパッチ＋entrypointのADB_TCP=1で開ける。

対象は再起動を超えて/dataに残るものとする。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 1VMあたりの上限（分）。超えたら中断し、次サイクルで再試行する。
BUDGET_MINUTES = 20.0
#: ADB到達待ちのポーリング間隔（秒）
POLL_INTERVAL = 5.0
#: ADB到達待ちの上限（秒）。起動直後の端末を待つ。
ADB_WAIT_TIMEOUT = 300.0
#: コンテナ側ADBポート（entrypointのANDROID_ADB_PORT既定と同一）。
ADB_PORT = 5555


class AndroidProvisioner:
    """ADBでAndroid端末を初期設定する。

    Args:
        pool: VmPool（省略時は遅延解決）。
        state_file: 設定済み記録の保存先。省略時は ``$DATA_DIR/android-provision.json``。
        is_running_check: ``(vm_id) -> bool``（実行中タスク有無）。あれば競合回避する。
        nap: 待機関数。テストで差し替え。
        apks_dir: 導入APK置場。省略時は ``$ANDROID_APKS_DIR`` か backend既定。
        adb_port: コンテナ側ADBポート。
    """

    def __init__(
        self,
        pool: Any | None = None,
        state_file: str | Path | None = None,
        is_running_check: Callable[[str], bool] | None = None,
        nap: Callable[[float], None] | None = None,
        apks_dir: str | Path | None = None,
        adb_port: int = ADB_PORT,
    ) -> None:
        self._pool = pool
        base = os.environ.get("DATA_DIR", "./data")
        self._state_file = Path(state_file or Path(base) / "android-provision.json")
        self._is_running_check = is_running_check
        self._nap = nap or time.sleep
        self._apks_dir = Path(
            apks_dir or os.environ.get("ANDROID_APKS_DIR", "/app/android-host/apks")
        )
        self._adb_port = adb_port
        self._task: asyncio.Task | None = None
        self._running = False

    # ── 常駐ループ ────────────────────────────────

    async def start(self, interval: float = 120.0) -> None:
        """バックグラウンド監視を開始する（ANDROID_PROVISION=0 で無効）。"""
        if os.environ.get("ANDROID_PROVISION", "1").lower() in ("0", "false", "no", ""):
            logger.info("Android provisioning は無効")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(interval))
        logger.info("Android provisioning を開始")

    async def stop(self) -> None:
        import contextlib

        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _run(self, interval: float) -> None:
        try:
            while self._running:
                try:
                    await asyncio.to_thread(self.ensure)
                except Exception:
                    logger.exception("Android provisioning 巡回でエラー")
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Android provisioning ループでエラー")

    # ── 対象解決・状態 ────────────────────────────

    def _pool_or_create(self) -> Any:
        if self._pool is None:
            from ai_desktop_agent.server.vm_pool import VmPool

            self._pool = VmPool()
        return self._pool

    def _targets(self) -> list[tuple[str, str]]:
        """[(vm_id, host)] 稼働中Androidの一覧。失敗時は []。"""
        try:
            from ai_desktop_agent.server.vm_pool import ANDROID_SERVICE, KIND_ANDROID

            pool = self._pool_or_create()
            out = []
            for vm in pool.list_vms():
                if vm.kind != KIND_ANDROID or vm.status != "running":
                    continue
                host = ANDROID_SERVICE if vm.id == ANDROID_SERVICE else (vm.vnc_host or vm.id)
                out.append((vm.id, host))
            return out
        except Exception:
            logger.debug("Android一覧の取得に失敗", exc_info=True)
            return []

    def _data_id(self, vm_id: str) -> str:
        """/data の同一性（変われば再設定）。固定はホスト実体、動的は開始時刻。"""
        try:
            from ai_desktop_agent.server.vm_pool import ANDROID_SERVICE

            if vm_id == ANDROID_SERVICE:
                for cand in (
                    "/app/android-host/android-data.img",
                    f"{os.environ.get('VM_HOST_DIR', '.')}/android/android-data.img",
                ):
                    try:
                        st = os.stat(cand)
                        return f"{st.st_size}:{st.st_mtime_ns}"
                    except OSError:
                        continue
                return "missing"
            info = self._pool_or_create().get_vm(vm_id)
            if info is None:
                return "gone"
            return f"running:{vm_id}"
        except Exception:
            logger.debug("data識別に失敗: %s", vm_id, exc_info=True)
            return "unknown"

    def _load_state(self) -> dict:
        try:
            import json as _json

            return _json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            import json as _json

            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(_json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.warning("provision状態の保存に失敗", exc_info=True)

    def needs(self, vm_id: str) -> bool:
        """未設定なら True。"""
        state = self._load_state()
        rec = state.get(vm_id)
        if not rec or not rec.get("done"):
            return True
        return rec.get("data_id") != self._data_id(vm_id)

    def mark_done(self, vm_id: str) -> None:
        import time as _time

        state = self._load_state()
        state[vm_id] = {"done": True, "data_id": self._data_id(vm_id), "at": _time.time()}
        self._save_state(state)

    def ensure(self) -> None:
        """未設定の稼働Androidを1台ずつ設定する（同期的・長時間）。"""
        for vm_id, host in self._targets():
            if not self.needs(vm_id):
                continue
            if self._is_running_check is not None:
                try:
                    if self._is_running_check(vm_id):
                        logger.info("タスク実行中のためprovisionを延期: %s", vm_id)
                        continue
                except Exception:
                    logger.debug("busy判定に失敗", exc_info=True)
            logger.info("Android provisioning 開始: %s", vm_id)
            try:
                if self.provision_vm(host):
                    self.mark_done(vm_id)
                    logger.info("Android provisioning 完了: %s", vm_id)
                else:
                    logger.warning("Android provisioning 未完: %s（次回再試行）", vm_id)
            except Exception:
                logger.exception("Android provisioning 失敗: %s", vm_id)

    # ── 本体（ADBのみ・VNCタップ列なし） ──────────

    def _wait_adb_serial(self, host: str, timeout: float = ADB_WAIT_TIMEOUT) -> str | None:
        """ADB接続できるまで待ち、シリアルを返す。期限切れは None。"""
        from ai_desktop_agent.server import android_adb as adb

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                serial = adb.connect(host, self._adb_port)
            except Exception:
                logger.debug("ADB接続に失敗、再試行", exc_info=True)
                serial = None
            if serial is not None:
                return serial
            self._nap(POLL_INTERVAL)
        return None

    def _apk_paths(self) -> list[Path]:
        """導入対象のAPK一覧。置場不在時は []。"""
        try:
            return sorted(self._apks_dir.glob("*.apk"))
        except OSError:
            logger.debug("APK置場を読めない: %s", self._apks_dir, exc_info=True)
            return []

    def provision_vm(self, host: str) -> bool:
        """1台をADBで初期設定する。成功時 True。

        手順はADBのshell命令のみとし、VNCタップ列は使わない。
        - スリープしない設定（screen_off_timeout最大・stay_on・screensaver無効）
        - タイムゾーン（Asia/Tokyo）
        - ネットワーク: VIRT_WIFI=0既定ではeth0がDHCPで自動取得するため
          Wi-Fi接続は必須としない。wifi失敗でもeth0/wlan0にIPv4があれば成功扱い。
          (9.0の `cmd wifi` はshell権限で使えず、wlan0自体が無い構成のため)
        - HOME固定（Launcher等を自動選択）
        - APK導入（置場の *.apk を adb install）
        """
        from ai_desktop_agent.server import android_adb as adb

        deadline = time.monotonic() + BUDGET_MINUTES * 60
        serial = self._wait_adb_serial(host)
        if serial is None:
            logger.warning("ADB到達不能: %s（次回再試行）", host)
            return False
        results: dict[str, bool] = {}
        results["sleep"] = adb.setup_sleep(serial)
        results["timezone"] = adb.setup_timezone(serial)
        wifi_ok = adb.setup_wifi(serial)
        if wifi_ok:
            results["wifi"] = True
        elif adb.has_network(serial):
            logger.info("Wi-Fi未設定だがEthernet疎通あり、成功扱い: %s", host)
            results["wifi"] = True
        else:
            results["wifi"] = False
        results["home"] = adb.setup_home(serial)
        # 初回ダイアログ残留をADBのHOMEキーで消す(VNC不使用、失敗無害)
        try:
            adb.dismiss_dialogs(serial)
        except Exception:
            logger.debug("dialog-dismissに失敗", exc_info=True)
        apks = self._apk_paths()
        if not apks:
            logger.info("APKなし、導入を省略: %s", host)
        apk_ok = True
        for apk in apks:
            if time.monotonic() > deadline:
                logger.warning("時間切れ: %s", host)
                return False
            if not adb.install_apk(serial, str(apk)):
                apk_ok = False
        results["apks"] = apk_ok
        for key, val in results.items():
            logger.info("step %s -> %s", key, val)
        ok = all(results.values())
        if not ok:
            logger.warning("Android provisioning 未完: %s（次回再試行）", host)
        return ok
