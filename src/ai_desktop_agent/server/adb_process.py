"""ADB subprocesses with cooperative cancellation and a shared attempt deadline."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress

from ai_desktop_agent.server import android_adb

TARGET_CHECK_INTERVAL = 1.0
TARGET_CHECK_TIMEOUT = 1.0


class AdbRunner:
    def __init__(
        self,
        deadline: float,
        cancelled: Callable[[], bool],
        *,
        before_execute: Callable[[], bool] | None = None,
    ) -> None:
        self.deadline = deadline
        self.cancelled = cancelled
        self.before_execute = before_execute
        self._failed = False
        self._validator: ThreadPoolExecutor | None = None
        self._validation: Future[bool] | None = None
        self._validation_started = 0.0
        self._next_validation = 0.0

    def allowed(self) -> bool:
        return not self._failed and not self.cancelled() and time.monotonic() < self.deadline

    def close(self) -> None:
        # Drain read-only validation before releasing the attempt to another owner.
        if self._validator is not None:
            self._validator.shutdown(wait=True)
            self._validator = None
        if self._validation is not None:
            try:
                if not self._validation.result():
                    self._failed = True
            except Exception:
                self._failed = True
            self._validation = None

    def _finish_validation(self, *, wait: bool) -> bool:
        future = self._validation
        if future is None:
            return True
        if not wait and not future.done():
            if time.monotonic() - self._validation_started >= TARGET_CHECK_TIMEOUT:
                self._failed = True
                return False
            return True
        try:
            valid = future.result(
                timeout=min(TARGET_CHECK_TIMEOUT, max(0, self.deadline - time.monotonic()))
            )
        except TimeoutError:
            self._failed = True
            return False
        except BaseException:
            self._failed = True
            raise
        self._validation = None
        self._next_validation = time.monotonic() + TARGET_CHECK_INTERVAL
        if not valid:
            self._failed = True
        return valid

    def _validate_during_command(self) -> bool:
        if self.before_execute is None:
            return True
        if not self._finish_validation(wait=False):
            return False
        now = time.monotonic()
        if self._validation is None and now >= self._next_validation:
            if self._validator is None:
                self._validator = ThreadPoolExecutor(max_workers=1, thread_name_prefix="adb-target")
            self._validation_started = now
            self._validation = self._validator.submit(self.before_execute)
        return True

    def _execute(self, arguments: list[str], timeout: float):
        executable = android_adb._adb_bin()
        if executable is None or not self.allowed():
            return None
        if self.before_execute is not None and (
            not self._finish_validation(wait=True) or not self.before_execute()
        ):
            self._failed = True
            return None
        if not self.allowed():
            return None
        deadline = min(self.deadline, time.monotonic() + timeout)
        self._next_validation = time.monotonic() + TARGET_CHECK_INTERVAL
        try:
            with subprocess.Popen(
                [executable, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                start_new_session=True,
            ) as process:
                finished = False
                try:
                    while self.allowed():
                        # Docker reads must not block child cancellation or its deadline.
                        if not self._validate_during_command():
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        try:
                            out, err = process.communicate(timeout=min(0.1, remaining))
                            finished = True
                            return subprocess.CompletedProcess(
                                arguments, process.returncode, out, err
                            )
                        except subprocess.TimeoutExpired:
                            continue
                finally:
                    if not finished:
                        # The shared adb server has its own session; leave it available.
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.communicate(timeout=0.3)
                        except subprocess.TimeoutExpired:
                            with suppress(ProcessLookupError):
                                os.killpg(process.pid, signal.SIGKILL)
                            process.communicate()
        except (OSError, subprocess.SubprocessError):
            return None
        return None

    def run(self, serial: str, *arguments: str) -> bool:
        result = self._execute(["-s", serial, *arguments], android_adb.ADB_TIMEOUT)
        return result is not None and result.returncode == 0

    def query(self, serial: str, *arguments: str, timeout: float = android_adb.ADB_TIMEOUT):
        result = self._execute(["-s", serial, *arguments], timeout)
        return result.stdout.strip() if result is not None and result.returncode == 0 else None

    def connect(self, serial: str, *_arguments: str) -> bool:
        result = self._execute(["connect", serial], android_adb.ADB_TIMEOUT)
        if result is None or result.returncode != 0:
            return False
        message = (result.stdout + "\n" + result.stderr).lower()
        return "connected to" in message or "already connected" in message
