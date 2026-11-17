"""QEMU monitor client with bounded, correlated responses over a UNIX socket."""

from __future__ import annotations

import json
import math
import socket
import threading
import time
from contextlib import suppress

MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class QmpError(RuntimeError):
    """QMP通信エラー。"""


class QmpClient:
    """Serialize monitor commands and discard unsolicited events and unmatched replies."""

    def __init__(self, path: str, timeout: float = 5.0) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("QMP timeout must be finite and positive")
        self._path = path
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._buffer = bytearray()
        self._request_id = 0
        self._lock = threading.RLock()

    def __enter__(self) -> QmpClient:
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def connect(self) -> None:
        """Negotiate capabilities within one deadline; close every failed connection."""
        with self._lock:
            self.close()
            deadline = time.monotonic() + self._timeout
            try:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._set_deadline(deadline)
                self._sock.connect(self._path)
                greeting = self._read_message(deadline)
                if not isinstance(greeting.get("QMP"), dict):
                    raise QmpError("QMPグリーティング不正")
                result = self._exchange("qmp_capabilities", None, deadline)
                if "error" in result:
                    raise QmpError(f"capabilities失敗: {result['error']}")
            except (OSError, QmpError) as error:
                self.close()
                raise QmpError(f"QMP接続に失敗: {self._path}: {error}") from error

    def execute(self, command: str, arguments: dict | None = None) -> dict:
        """Return the response for this command, never an asynchronous monitor event."""
        with self._lock:
            if self._sock is None:
                raise QmpError("未接続です")
            try:
                return self._exchange(command, arguments, time.monotonic() + self._timeout)
            except (OSError, QmpError) as error:
                # A late reply must never be consumed by the next command.
                self.close()
                raise QmpError(f"QMP送受信に失敗: {error}") from error

    def _exchange(self, command: str, arguments: dict | None, deadline: float) -> dict:
        assert self._sock is not None
        self._request_id += 1
        request_id = self._request_id
        payload: dict = {"execute": command, "id": request_id}
        if arguments is not None:
            payload["arguments"] = arguments
        encoded = (json.dumps(payload) + "\r\n").encode("utf-8")
        self._set_deadline(deadline)
        self._sock.sendall(encoded)
        while True:
            response = self._read_message(deadline)
            if "event" in response:
                continue
            if type(response.get("id")) is not int or response["id"] != request_id:
                continue
            if ("return" in response) == ("error" in response):
                raise QmpError("QMP応答にreturnまたはerrorが必要です")
            return response

    def query_status(self) -> dict:
        """VM稼働状態を返す。"""
        with self._lock:
            result = self.execute("query-status")
            if "error" in result:
                raise QmpError(f"query-status失敗: {result['error']}")
            status = result["return"]
            if not isinstance(status, dict) or not isinstance(status.get("status"), str):
                self.close()
                raise QmpError("query-statusの応答形式が不正です")
            return status

    def close(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
            self._buffer.clear()
            if sock is not None:
                with suppress(OSError):
                    sock.close()

    def _set_deadline(self, deadline: float) -> None:
        assert self._sock is not None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QmpError("QMP応答待ちが上限に達しました")
        self._sock.settimeout(remaining)

    def _read_message(self, deadline: float) -> dict:
        assert self._sock is not None
        while True:
            self._set_deadline(deadline)
            end = self._buffer.find(b"\n")
            if end >= 0:
                if end > MAX_MESSAGE_BYTES:
                    raise QmpError("QMP応答がサイズ上限を超えました")
                line = self._buffer[:end]
                del self._buffer[: end + 1]
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError, RecursionError) as error:
                    raise QmpError("QMP応答のJSONが不正です") from error
                if not isinstance(message, dict):
                    raise QmpError("QMP応答はJSONオブジェクトである必要があります")
                return message
            if len(self._buffer) > MAX_MESSAGE_BYTES:
                raise QmpError("QMP応答がサイズ上限を超えました")
            chunk = self._sock.recv(min(65536, MAX_MESSAGE_BYTES + 1 - len(self._buffer)))
            if not chunk:
                raise QmpError("QMP接続が切断されました")
            self._buffer.extend(chunk)
