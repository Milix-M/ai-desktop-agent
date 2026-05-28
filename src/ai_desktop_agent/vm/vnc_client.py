"""vncdotool を用いたVNCバックエンド実装。

DisplayBackend インターフェースの具象クラス。
vncdotool の ThreadedVNCClientProxy をラップして同期的に操作する。
"""

import io
import logging
import time
from typing import Any

from ai_desktop_agent.vm.base import DisplayBackend
from ai_desktop_agent.vm.screenshot import Screenshot

logger = logging.getLogger(__name__)

# マウスボタン定数 (vncdotool)
_BUTTON_LEFT = 1
_BUTTON_MIDDLE = 2
_BUTTON_RIGHT = 3

# スクロールボタン
_BUTTON_SCROLL_UP = 4
_BUTTON_SCROLL_DOWN = 5

# キー名マッピング: Actionキー名 → vncdotoolキー名
_KEY_MAP: dict[str, str] = {
    "enter": "enter",
    "return": "enter",
    "escape": "escape",
    "esc": "escape",
    "tab": "tab",
    "space": "space",
    "backspace": "backspace",
    "delete": "delete",
    "home": "home",
    "end": "end",
    "page_up": "pageup",
    "page_down": "pagedown",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "f1": "f1",
    "f2": "f2",
    "f3": "f3",
    "f4": "f4",
    "f5": "f5",
    "f6": "f6",
    "f7": "f7",
    "f8": "f8",
    "f9": "f9",
    "f10": "f10",
    "f11": "f11",
    "f12": "f12",
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "super": "super",
    "win": "super",
    "cmd": "super",
    "insert": "insert",
    "print_screen": "print",
}


class VNCClient(DisplayBackend):
    """vncdotool をラップした同期VNCクライアント。

    ThreadedVNCClientProxy を使用するため、非同期コンテキスト不要で
    同期的に操作できる。

    Usage:
        client = VNCClient()
        client.connect("localhost", 5900)
        screenshot = client.capture_screen()
        client.mouse_click(100, 200)
        client.type_text("hello")
        client.disconnect()
    """

    def __init__(self) -> None:
        self._client: Any = None  # ThreadedVNCClientProxy
        self._connected = False
        self._frame_count = 0
        self._width = 0
        self._height = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── 接続管理 ───────────────────────────────────────

    def connect(self, host: str, port: int = 5900, password: str | None = None) -> None:
        """VNCサーバーに接続する。"""
        from vncdotool import api

        server = f"{host}:{port}"
        logger.info("VNC接続中: %s", server)

        self._client = api.connect(
            server,
            password=password,
            timeout=10.0,
        )
        self._connected = True
        logger.info("VNC接続完了: %s", server)

    def disconnect(self) -> None:
        """VNC接続を切断する。"""
        if self._client is not None:
            try:  # noqa: SIM105
                self._client.disconnect()
            except Exception:
                pass
        self._client = None
        self._connected = False
        self._frame_count = 0

    # ── 画面キャプチャ ─────────────────────────────────

    def capture_screen(self) -> Screenshot:
        """画面全体をキャプチャ。"""
        self._ensure_connected()

        buf = io.BytesIO()
        self._client.captureScreen(buf)
        data = buf.getvalue()
        self._frame_count += 1

        return Screenshot(
            image_bytes=data,
            width=self._width or 1024,
            height=self._height or 768,
            timestamp=time.monotonic(),
            frame_number=self._frame_count,
        )

    def capture_region(self, x: int, y: int, width: int, height: int) -> Screenshot:
        """指定領域をキャプチャ。"""
        self._ensure_connected()

        buf = io.BytesIO()
        self._client.captureRegion(buf, x, y, width, height)
        data = buf.getvalue()
        self._frame_count += 1

        return Screenshot(
            image_bytes=data,
            width=width,
            height=height,
            timestamp=time.monotonic(),
            frame_number=self._frame_count,
        )

    # ── マウス操作 ─────────────────────────────────────

    def mouse_move(self, x: int, y: int) -> None:
        self._ensure_connected()
        self._client.mouseMove(x, y)

    def mouse_down(self, button: int = 1) -> None:
        self._ensure_connected()
        self._client.mouseDown(button)

    def mouse_up(self, button: int = 1) -> None:
        self._ensure_connected()
        self._client.mouseUp(button)

    def mouse_click(self, x: int | None = None, y: int | None = None, button: int = 1) -> None:
        """指定座標をクリック（move → press → release）。"""
        if x is not None and y is not None:
            self.mouse_move(x, y)
        self.mouse_down(button)
        self.mouse_up(button)

    def mouse_double_click(
        self, x: int | None = None, y: int | None = None, button: int = 1
    ) -> None:
        """ダブルクリック。"""
        self.mouse_click(x, y, button)
        self.mouse_click(button=button)

    def mouse_drag(
        self, start_x: int, start_y: int, end_x: int, end_y: int, button: int = 1
    ) -> None:
        """ドラッグ操作。"""
        self._ensure_connected()
        self._client.mouseMove(start_x, start_y)
        self._client.mouseDown(button)
        self._client.mouseDrag(end_x, end_y, step=10)
        self._client.mouseUp(button)

    def mouse_scroll(self, direction: str, amount: int = 1) -> None:
        self._ensure_connected()
        btn = _BUTTON_SCROLL_UP if direction == "up" else _BUTTON_SCROLL_DOWN
        for _ in range(abs(amount)):
            self._client.mouseDown(btn)
            self._client.mouseUp(btn)

    # ── キーボード操作 ─────────────────────────────────

    def key_press(self, key: str) -> None:
        """キーを押して離す。"""
        self._ensure_connected()
        mapped = self._map_key(key)
        self._client.keyPress(mapped)

    def key_down(self, key: str) -> None:
        """キーを押し続ける。"""
        self._ensure_connected()
        mapped = self._map_key(key)
        self._client.keyDown(mapped)

    def key_up(self, key: str) -> None:
        """キーを離す。"""
        self._ensure_connected()
        mapped = self._map_key(key)
        self._client.keyUp(mapped)

    def key_combo(self, keys: list[str]) -> None:
        """キーコンビネーション（Ctrl+C 等）。"""
        self._ensure_connected()
        mapped = [self._map_key(k) for k in keys]
        # すべて押す
        for k in mapped:
            self._client.keyDown(k)
        # 逆順で離す
        for k in reversed(mapped):
            self._client.keyUp(k)

    def type_text(self, text: str) -> None:
        """テキストを貼り付け（クリップボード経由で高速入力）。"""
        self._ensure_connected()
        self._client.paste(text)

    # ── 内部 ──────────────────────────────────────────

    def _ensure_connected(self) -> None:
        if not self._connected or self._client is None:
            raise RuntimeError("VNCに接続されていません")

    @staticmethod
    def _map_key(key: str) -> str:
        """キー名をvncdotool形式に変換。"""
        return _KEY_MAP.get(key.lower(), key.lower())
