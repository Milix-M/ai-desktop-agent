"""QEMU guest agent クライアント — ゲスト内情報取得・コマンド実行。

ゲストに qemu-guest-agent が必要（build-vm-image.sh で導入）。
virtio-serial チャネル経由の UNIX ソケットに接続する。
プロトコルは改行区切りJSON（QMPと異なりグリーティング不要）。
"""

from __future__ import annotations

import base64
import json
import logging
import socket
import time

logger = logging.getLogger(__name__)


class QgaError(RuntimeError):
    """guest agent 通信エラー。"""


class QgaClient:
    """qemu-guest-agent クライアント（コンテキストマネージャ対応）。"""

    def __init__(self, path: str, timeout: float = 10.0) -> None:
        self._path = path
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._file = None

    def __enter__(self) -> QgaClient:
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def connect(self) -> None:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self._timeout)
            sock.connect(self._path)
        except OSError as e:
            raise QgaError(f"guest agent接続に失敗: {self._path}: {e}") from e
        self._sock = sock
        self._file = sock.makefile("r", encoding="utf-8")

    def execute(self, command: str, arguments: dict | None = None) -> dict:
        """guest-agent コマンドを実行して応答dictを返す。"""
        if self._sock is None or self._file is None:
            raise QgaError("未接続です")
        payload: dict = {"execute": command}
        if arguments:
            payload["arguments"] = arguments
        try:
            self._sock.sendall((json.dumps(payload) + "\n").encode())
            line = self._file.readline()
            if not line:
                raise QgaError("guest agent接続が切断されました")
        except OSError as e:
            raise QgaError(f"送受信に失敗: {e}") from e
        try:
            data = json.loads(line)
        except json.JSONDecodeError as e:
            raise QgaError(f"応答のパースに失敗: {line[:120]}") from e
        if "error" in data:
            raise QgaError(f"{command}失敗: {data['error']}")
        return data.get("return", {})

    def ping(self) -> bool:
        """疎通確認。"""
        self.execute("guest-ping")
        return True

    def get_osinfo(self) -> dict:
        """ゲストOS情報を返す。"""
        return self.execute("guest-get-osinfo")

    def exec_status(self, pid: int) -> dict:
        """guest-exec の実行結果を取得する。"""
        return self.execute("guest-exec-status", {"pid": pid})

    def run(
        self,
        command: list[str] | str,
        input_data: bytes | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, str, str]:
        """ゲスト内でコマンドを実行する。

        Returns:
            (終了コード, stdout, stderr)。出力は最大16KBまで。
        """
        if isinstance(command, str):
            command = ["/bin/sh", "-c", command]
        args: dict = {"path": command[0], "arg": command[1:], "capture-output": True}
        if input_data:
            args["input-data"] = base64.b64encode(input_data).decode()
        started = self.execute("guest-exec", args)
        pid = started.get("pid")
        if pid is None:
            raise QgaError(f"guest-exec開始失敗: {started}")
        deadline = time.monotonic() + timeout
        while True:
            st = self.exec_status(int(pid))
            if st.get("exited", False):
                out = base64.b64decode(st.get("out-data", "") or "").decode("utf-8", "replace")[
                    :16384
                ]
                err = base64.b64decode(st.get("err-data", "") or "").decode("utf-8", "replace")[
                    :16384
                ]
                return int(st.get("exitcode", -1)), out, err
            if time.monotonic() > deadline:
                raise QgaError(f"guest-execタイムアウト: {command}")
            time.sleep(0.5)

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
