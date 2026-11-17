"""Cancellable Android initialization, with exclusive handover to interactive tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from ai_desktop_agent.server.adb_process import AdbRunner
from ai_desktop_agent.server.persistence import StorageError, atomic_json, metadata_lock
from ai_desktop_agent.vm.async_io import run_display_call, vm_call

logger = logging.getLogger(__name__)

BUDGET_MINUTES = 20.0
POLL_INTERVAL = 5.0
ADB_WAIT_TIMEOUT = 300.0
ADB_PORT = 5555


@dataclass
class _Attempt:
    cancelled: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True)
class ProvisionTarget:
    host: str
    container_id: str
    data_id: str


class _TargetCheck:
    def __init__(self, check: Callable[[], bool], stopped: Callable[[], bool]):
        self._check = check
        self._stopped = stopped
        self._valid = True

    def current(self) -> bool:
        if self.interrupted():
            return False
        try:
            if not self._check() or self._stopped():
                self._valid = False
        except BaseException:
            self._valid = False
            raise
        return self._valid

    def interrupted(self) -> bool:
        if self._stopped():
            self._valid = False
        return not self._valid


class AndroidProvisioner:
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
        self._nap = nap
        self._apks_dir = Path(
            apks_dir or os.environ.get("ANDROID_APKS_DIR", "/app/android-host/apks")
        )
        self._adb_port = adb_port
        self._task: asyncio.Task | None = None
        self._running = False
        self._stop_requested = threading.Event()
        self._ensure_lock = threading.Lock()
        self._control_lock = threading.RLock()
        self._lifecycle_lock = asyncio.Lock()
        self._attempts: dict[str, _Attempt] = {}
        self._suspended: dict[str, int] = {}

    async def start(self, interval: float = 120.0) -> None:
        async with self._lifecycle_lock:
            if os.environ.get("ANDROID_PROVISION", "1").lower() in ("0", "false", "no", ""):
                return
            if not math.isfinite(interval) or interval <= 0:
                raise ValueError("provisioning interval must be finite and positive")
            if self._running:
                return
            self._stop_requested.clear()
            self._running = True
            self._task = asyncio.create_task(self._run(interval))

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._running = False
            self._stop_requested.set()
            with self._control_lock:
                attempts = list(self._attempts.values())
                for attempt in attempts:
                    attempt.cancelled.set()
            task = self._task
            cancelled = False
            if task is not None:
                task.cancel()
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        cancelled |= bool(asyncio.current_task().cancelling())
                    except Exception:
                        logger.exception("Android provisioningの終了に失敗")
                if not task.cancelled():
                    task.exception()
                self._task = None
            for attempt in attempts:
                try:
                    await run_display_call(attempt.done.wait)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError

    async def _run(self, interval: float) -> None:
        try:
            while self._running and not self._stop_requested.is_set():
                try:
                    await run_display_call(self.ensure)
                except Exception:
                    logger.exception("Android provisioning巡回でエラー")
                await asyncio.sleep(interval)
        finally:
            self._running = False

    @asynccontextmanager
    async def suspend(self, vm_id: str):
        """Stop and drain this VM's initialization before yielding exclusive access."""
        with self._control_lock:
            self._suspended[vm_id] = self._suspended.get(vm_id, 0) + 1
            attempt = self._attempts.get(vm_id)
            if attempt is not None:
                attempt.cancelled.set()
        try:
            if attempt is not None:
                await run_display_call(attempt.done.wait)
            yield
        finally:
            with self._control_lock:
                remaining = self._suspended[vm_id] - 1
                if remaining:
                    self._suspended[vm_id] = remaining
                else:
                    self._suspended.pop(vm_id)

    def _busy(self, vm_id: str) -> bool:
        try:
            return bool(self._is_running_check and self._is_running_check(vm_id))
        except Exception:
            logger.warning("タスクの状態を確認できないため初期設定を延期: %s", vm_id)
            return True

    def _pool_or_create(self):
        if self._pool is None:
            from ai_desktop_agent.server.vm_pool import VmPool

            self._pool = VmPool()
        return self._pool

    def _targets(self):
        from ai_desktop_agent.server.vm_pool import KIND_ANDROID

        return [
            (vm.id, vm.vnc_host or vm.id)
            for vm in self._pool_or_create().list_vms()
            if vm.kind == KIND_ANDROID and vm.status == "running"
        ]

    def _target(self, vm_id: str) -> ProvisionTarget | None:
        from ai_desktop_agent.server.vm_pool import KIND_ANDROID

        pool = self._pool_or_create()
        before = pool.get_instance(vm_id)
        if before is None or before.info.kind != KIND_ANDROID or before.info.status != "running":
            return None
        pool.prepare_vm(vm_id)
        after = pool.get_instance(vm_id)
        if (
            after is None
            or after.container_id != before.container_id
            or after.info.kind != KIND_ANDROID
            or after.info.status != "running"
        ):
            return None
        identity = self._data_id(vm_id, container_id=after.container_id)
        if identity is None:
            return None
        return ProvisionTarget(after.info.vnc_host or vm_id, after.container_id, identity)

    def _matches_target(self, vm_id: str, target: ProvisionTarget) -> bool:
        from ai_desktop_agent.server.vm_pool import KIND_ANDROID

        current = self._pool_or_create().get_instance(vm_id)
        if (
            current is None
            or current.container_id != target.container_id
            or current.info.kind != KIND_ANDROID
            or current.info.status != "running"
            or (current.info.vnc_host or vm_id) != target.host
        ):
            return False
        return self._data_id(vm_id, container_id=current.container_id) == target.data_id

    def _data_id(self, vm_id: str, *, container_id: str | None = None) -> str | None:
        from ai_desktop_agent.server.vm_pool import ANDROID_SERVICE

        if vm_id == ANDROID_SERVICE:
            for image in (
                Path("/app/android-host/android-data.img"),
                Path(os.environ.get("VM_HOST_DIR", ".")) / "android/android-data.img",
            ):
                try:
                    with image.open("rb") as file:
                        # ext4's superblock starts at byte 1024; UUID is at offset 0x68.
                        # https://www.kernel.org/doc/html/v7.0/filesystems/ext4/super.html
                        file.seek(1024)
                        block = file.read(0x78)
                    if block[0x38:0x3A] == b"\x53\xef" and len(block) == 0x78:
                        volume_uuid = block[0x68:0x78]
                        if any(volume_uuid):
                            return "ext4:" + str(uuid.UUID(bytes=volume_uuid))
                except OSError:
                    continue
        identity = (
            container_id
            if container_id is not None
            else vm_call(self._pool_or_create().instance_id, vm_id)
        )
        return "container:" + identity if identity else None

    def _load_state(self) -> dict:
        try:
            state = json.loads(self._state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            raise StorageError("Android初期設定の記録を読み込めません") from error
        if not isinstance(state, dict) or any(
            not isinstance(record, dict)
            or not isinstance(record.get("done"), bool)
            or not isinstance(record.get("data_id"), str)
            for record in state.values()
        ):
            raise StorageError("Android初期設定の記録の形式が不正です")
        return state

    def needs(self, vm_id: str) -> bool:
        record = self._load_state().get(vm_id)
        return not record or not record["done"] or record["data_id"] != self._data_id(vm_id)

    def mark_done(self, vm_id: str, *, data_id: str | None = None) -> None:
        identity = data_id or self._data_id(vm_id)
        if identity is None:
            raise StorageError("Androidの保存領域を識別できません")
        with metadata_lock(self._state_file):
            state = self._load_state()
            state[vm_id] = {"done": True, "data_id": identity, "at": time.time()}
            atomic_json(self._state_file, state)

    def ensure(self) -> None:
        with self._ensure_lock:
            for vm_id, _host in vm_call(self._targets):
                if self._stop_requested.is_set():
                    return
                if not self.needs(vm_id):
                    continue
                with self._control_lock:
                    if self._suspended.get(vm_id) or self._busy(vm_id):
                        continue
                    attempt = _Attempt()
                    self._attempts[vm_id] = attempt

                def cancelled(attempt=attempt, vm_id=vm_id):
                    return (
                        self._stop_requested.is_set()
                        or attempt.cancelled.is_set()
                        or self._busy(vm_id)
                    )

                try:
                    # Discovery may precede a deletion or replacement; revalidate after claiming.
                    target = vm_call(self._target, vm_id)
                    if target is None or cancelled():
                        continue
                    check = _TargetCheck(
                        partial(vm_call, self._matches_target, vm_id, target), cancelled
                    )
                    if not check.current():
                        continue
                    logger.info("Android provisioning開始: %s", vm_id)
                    completed = self.provision_vm(
                        target.host, cancelled=check.interrupted, before_execute=check.current
                    )
                    if not check.current():
                        logger.info("Android provisioning中断: %s", vm_id)
                    elif completed:
                        self.mark_done(vm_id, data_id=target.data_id)
                        logger.info("Android provisioning完了: %s", vm_id)
                    else:
                        logger.warning("Android provisioning未完: %s（次回再試行）", vm_id)
                except Exception:
                    logger.exception("Android provisioning失敗: %s", vm_id)
                finally:
                    with self._control_lock:
                        self._attempts.pop(vm_id, None)
                        attempt.done.set()

    def _wait_adb_serial(self, host: str, *, runner: AdbRunner) -> str | None:
        from ai_desktop_agent.server import android_adb as adb

        deadline = min(runner.deadline, time.monotonic() + ADB_WAIT_TIMEOUT)
        while runner.allowed() and time.monotonic() < deadline:
            serial = adb.connect(host, self._adb_port, runner=runner.connect)
            if serial is not None:
                return serial
            delay = min(POLL_INTERVAL, max(0, deadline - time.monotonic()))
            if self._nap is not None:
                self._nap(delay)
            else:
                until = time.monotonic() + delay
                while runner.allowed() and time.monotonic() < until:
                    time.sleep(min(0.05, max(0, until - time.monotonic())))
        return None

    def _apk_paths(self) -> list[Path]:
        return sorted(self._apks_dir.glob("*.apk"))

    def provision_vm(
        self,
        host: str,
        *,
        cancelled: Callable[[], bool] | None = None,
        before_execute: Callable[[], bool] | None = None,
    ) -> bool:
        def stopped():
            return self._stop_requested.is_set() or bool(cancelled and cancelled())

        runner = AdbRunner(
            time.monotonic() + BUDGET_MINUTES * 60, stopped, before_execute=before_execute
        )
        try:
            completed = self._provision_steps(host, runner)
        finally:
            runner.close()
        return completed and runner.allowed()

    def _provision_steps(self, host: str, runner: AdbRunner) -> bool:
        from ai_desktop_agent.server import android_adb as adb

        serial = self._wait_adb_serial(host, runner=runner)
        if serial is None or not runner.allowed():
            return False
        results = []
        for step in (adb.setup_sleep, adb.setup_timezone, adb.setup_wifi, adb.setup_home):
            if not runner.allowed():
                return False
            options = {"runner": runner.run}
            if step is adb.setup_home:
                options["query"] = runner.query
            ok = step(serial, **options)
            if step is adb.setup_wifi and not ok and runner.allowed():
                ok = adb.has_network(serial, query=runner.query)
            results.append(ok)
        if not runner.allowed():
            return False
        adb.dismiss_dialogs(serial, runner=runner.run)
        for apk in self._apk_paths():
            if not runner.allowed():
                return False
            results.append(
                adb.install_apk(serial, str(apk), query=partial(runner.query, timeout=120.0))
            )
        return runner.allowed() and all(results)
