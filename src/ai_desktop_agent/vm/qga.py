"""Synchronized, bounded QEMU guest-agent calls over a UNIX socket."""

from __future__ import annotations

import base64
import json
import math
import secrets
import socket
import threading
import time
from contextlib import contextmanager, suppress

from ai_desktop_agent.privacy import redact

MAX_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024


class QgaError(RuntimeError):
    def __init__(self, message: str, *, pid: int | None = None):
        super().__init__(message)
        self.pid = pid


class QgaTimeoutError(QgaError):
    """Waiting expired; a remote command may still be running."""


class QgaOutputLimitError(QgaError):
    def __init__(self, stdout: str, stderr: str, *, exitcode: int):
        super().__init__("guest-execの出力が省略されています（各出力の上限は16 KiB）")
        self.stdout = stdout
        self.stderr = stderr
        self.exitcode = exitcode


class QgaClient:
    def __init__(self, path: str, timeout: float = 10.0) -> None:
        self._validate_timeout(timeout)
        self._path = path
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._buffer = bytearray()
        self._lock = threading.RLock()
        self._request_id = 0

    def __enter__(self) -> QgaClient:
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @staticmethod
    def _validate_timeout(timeout: float) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("guest-agent timeout must be finite and positive")

    @contextmanager
    def _operation(self, timeout: float):
        self._validate_timeout(timeout)
        deadline = time.monotonic() + timeout
        if not self._lock.acquire(timeout=timeout):
            raise QgaTimeoutError("guest agentの操作順待ちが上限に達しました")
        try:
            yield deadline
        finally:
            self._lock.release()

    def connect(self) -> None:
        with self._operation(self._timeout) as deadline:
            self.close()
            try:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._set_deadline(deadline)
                self._sock.connect(self._path)
                # virtio-serial may retain a previous client's partial input and output.
                # https://www.qemu.org/docs/master/interop/qemu-ga-ref#command-guest-sync-delimited
                nonce = secrets.randbits(63)
                self._execute_until("guest-sync-delimited", {"id": nonce}, deadline, flush=True)
            except (OSError, QgaError) as error:
                self.close()
                raise QgaError(f"guest agent接続に失敗: {self._path}: {error}") from error

    def execute(self, command: str, arguments: dict | None = None):
        with self._operation(self._timeout) as deadline:
            return self._execute_until(command, arguments, deadline)

    def _execute_until(self, command: str, arguments: dict | None, deadline: float, *, flush=False):
        if self._sock is None:
            raise QgaError("未接続です")
        self._request_id += 1
        request_id = self._request_id
        payload = {"execute": command, "id": request_id}
        if arguments is not None:
            payload["arguments"] = arguments
        data = (json.dumps(payload, allow_nan=False) + "\n").encode("utf-8")
        if len(data) > MAX_MESSAGE_BYTES:
            raise ValueError("guest agentへの要求がサイズ上限を超えました")
        synchronizing = command == "guest-sync-delimited"
        deadline = min(deadline, time.monotonic() + self._timeout)
        try:
            self._set_deadline(deadline)
            self._sock.sendall((b"\xff" if flush else b"") + data)
            while True:
                if synchronizing:
                    self._seek_marker(deadline)
                message = self._read_message(deadline, synchronizing=synchronizing)
                if message is None or "event" in message:
                    continue
                if type(message.get("id")) is not int or message["id"] != request_id:
                    continue
                if ("return" in message) == ("error" in message):
                    raise QgaError("guest agent応答にreturnまたはerrorが必要です")
                if "error" in message:
                    if not isinstance(message["error"], dict):
                        raise QgaError("guest agentのエラー応答が不正です")
                    break
                if synchronizing and (
                    type(message["return"]) is not int or message["return"] != arguments["id"]
                ):
                    continue
                break
        except TimeoutError as error:
            self.close()
            raise QgaTimeoutError("guest agentの応答待ちが上限に達しました") from error
        except (OSError, QgaError):
            self.close()
            raise
        if "error" in message:
            raise QgaError("guest agentコマンドに失敗: " + redact(str(message["error"])))
        return message["return"]

    def ping(self) -> bool:
        self.execute("guest-ping")
        return True

    def _dictionary(self, value, command):
        if not isinstance(value, dict):
            self.close()
            raise QgaError(f"{command}の応答形式が不正です")
        return value

    def get_osinfo(self) -> dict:
        with self._operation(self._timeout) as deadline:
            return self._dictionary(
                self._execute_until("guest-get-osinfo", None, deadline), "OS情報"
            )

    def exec_status(self, pid: int) -> dict:
        if type(pid) is not int or pid <= 0:
            raise ValueError("guest process PID must be a positive integer")
        with self._operation(self._timeout) as deadline:
            return self._status(pid, deadline)

    def _status(self, pid, deadline):
        status = self._dictionary(
            self._execute_until("guest-exec-status", {"pid": pid}, deadline), "実行状態"
        )
        if type(status.get("exited")) is not bool:
            self.close()
            raise QgaError("guest-exec-statusのexitedが不正です")
        return status

    def _output(self, status, key):
        encoded = status.get(key, "")
        if not isinstance(encoded, str):
            self.close()
            raise QgaError("guest-execの出力形式が不正です")
        try:
            raw = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (ValueError, UnicodeError) as error:
            self.close()
            raise QgaError("guest-execの出力がbase64ではありません") from error
        return raw[:MAX_OUTPUT_BYTES].decode("utf-8", "replace"), len(raw) > MAX_OUTPUT_BYTES

    def run(
        self,
        command: list[str] | str,
        input_data: bytes | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, str, str]:
        """Wait within one deadline; expose PID on errors and partial output on output limits."""
        if isinstance(command, str):
            command = ["/bin/sh", "-c", command]
        if not command or not all(isinstance(arg, str) for arg in command) or not command[0]:
            raise ValueError("guest command must contain an executable and string arguments")
        args = {"path": command[0], "arg": command[1:], "capture-output": True}
        if input_data is not None:
            args["input-data"] = base64.b64encode(input_data).decode("ascii")
        pid = None
        with self._operation(timeout) as deadline:
            try:
                started = self._dictionary(
                    self._execute_until("guest-exec", args, deadline), "実行開始"
                )
                pid = started.get("pid")
                if type(pid) is not int or pid <= 0:
                    self.close()
                    raise QgaError("guest-execのPIDが不正です")
                while True:
                    status = self._status(pid, deadline)
                    if status["exited"]:
                        out, out_limited = self._output(status, "out-data")
                        err, err_limited = self._output(status, "err-data")
                        for name in ("out-truncated", "err-truncated"):
                            if type(status.get(name, False)) is not bool:
                                self.close()
                                raise QgaError("guest-execの省略情報が不正です")
                        code = status.get("exitcode")
                        if type(code) is not int:
                            signum = status.get("signal")
                            if type(signum) is not int or signum <= 0:
                                self.close()
                                raise QgaError("guest-execの終了状態が不正です")
                            code = -signum
                        if (
                            out_limited
                            or err_limited
                            or status.get("out-truncated")
                            or status.get("err-truncated")
                        ):
                            raise QgaOutputLimitError(out, err, exitcode=code)
                        return code, out, err
                    self._set_deadline(deadline)
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            except QgaError as error:
                error.pid = pid if type(pid) is int and pid > 0 else None
                if isinstance(error, QgaTimeoutError):
                    self.close()
                raise

    def close(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
            self._buffer.clear()
            if sock is not None:
                with suppress(OSError):
                    sock.close()

    def _set_deadline(self, deadline):
        assert self._sock is not None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QgaTimeoutError("guest agentの応答待ちが上限に達しました")
        self._sock.settimeout(remaining)

    def _receive(self, deadline):
        self._set_deadline(deadline)
        chunk = self._sock.recv(min(65536, MAX_MESSAGE_BYTES + 1 - len(self._buffer)))
        if not chunk:
            raise QgaError("guest agent接続が切断されました")
        self._buffer.extend(chunk)

    def _seek_marker(self, deadline):
        while True:
            self._set_deadline(deadline)
            marker = self._buffer.find(b"\xff")
            if marker >= 0:
                del self._buffer[: marker + 1]
                return
            self._buffer.clear()
            self._receive(deadline)

    def _read_message(self, deadline, *, synchronizing=False):
        while True:
            self._set_deadline(deadline)
            end = self._buffer.find(b"\n")
            marker = self._buffer.find(b"\xff") if synchronizing else -1
            if marker >= 0 and (end < 0 or marker < end):
                del self._buffer[: marker + 1]
                continue
            if end >= 0:
                if end > MAX_MESSAGE_BYTES:
                    raise QgaError("guest agent応答がサイズ上限を超えました")
                line = self._buffer[:end]
                del self._buffer[: end + 1]
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError, RecursionError) as error:
                    if synchronizing:
                        return None
                    raise QgaError("guest agent応答のJSONが不正です") from error
                if not isinstance(message, dict):
                    if synchronizing:
                        return None
                    raise QgaError("guest agent応答はJSONオブジェクトである必要があります")
                return message
            if len(self._buffer) > MAX_MESSAGE_BYTES:
                raise QgaError("guest agent応答がサイズ上限を超えました")
            self._receive(deadline)
