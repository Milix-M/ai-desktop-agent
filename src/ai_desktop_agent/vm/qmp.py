"""QMPクライアント — QEMU Machine Protocol の最小実装。

VMの稼働状態照会など、Dockerの外側からゲスト状態を取る。
"""

from __future__ import annotations

import json
import logging
import socket

logger = logging.getLogger(__name__)


class QmpError(RuntimeError):
    """QMP通信エラー。"""


class QmpClient:
    """UNIXソケット経由のQMPクライアント（コンテキストマネージャ対応）。"""

    def __init__(self, path: str, timeout: float = 5.0) -> None:
        self._path = path
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._file = None

    def __enter__(self) -> QmpClient:
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def connect(self) -> None:
        """接続して capabilities ネゴシエーションする。"""
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self._timeout)
            sock.connect(self._path)
        except OSError as e:
            raise QmpError(f"QMP接続に失敗: {self._path}: {e}") from e
        self._sock = sock
        self._file = sock.makefile("r", encoding="utf-8")
        greeting = self._readline()  # QMP greeting を読み飛ばす
        if "QMP" not in greeting:
            raise QmpError(f"QMPグリーティング不正: {greeting[:80]}")
        result = self.execute("qmp_capabilities")
        if "error" in result:
            raise QmpError(f"capabilities失敗: {result['error']}")

    def execute(self, command: str, arguments: dict | None = None) -> dict:
        """コマンドを実行して応答dictを返す。"""
        if self._sock is None or self._file is None:
            raise QmpError("未接続です")
        payload: dict = {"execute": command}
        if arguments:
            payload["arguments"] = arguments
        try:
            self._sock.sendall((json.dumps(payload) + "\n").encode())
            line = self._readline()
        except OSError as e:
            raise QmpError(f"QMP送受信に失敗: {e}") from e
        try:
            return json.loads(line)
        except json.JSONDecodeError as e:
            raise QmpError(f"QMP応答のパースに失敗: {line[:120]}") from e

    def query_status(self) -> dict:
        """VM稼働状態（status/endian等）を返す。"""
        result = self.execute("query-status")
        if "error" in result:
            raise QmpError(f"query-status失敗: {result['error']}")
        return result.get("return", {})

    def close(self) -> None:
        try:
            if self._file is not None:
                self._file.close()
        except Exception:
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        self._sock = None
        self._file = None

    def _readline(self) -> str:
        assert self._file is not None
        line = self._file.readline()
        if not line:
            raise QmpError("QMP接続が切断されました")
        return line
