"""vncdotool を用いたVNCバックエンド実装。

DisplayBackend インターフェースの具象クラス。
vncdotool の ThreadedVNCClientProxy をラップして同期的に操作する。
カーソル位置を内部追跡し、オーバーレイ付きスクリーンショットを生成できる。
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
    "page_up": "page_up",
    "page_down": "page_down",
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
    "print_screen": "print_screen",
}


def _ensure_vncdotool_keymap() -> None:
    """vncdotool の KEYMAP に欠けているキーを補う。

    KEYMAP 未登録の名前は _decodeKey が ord() でクラッシュする。
    また ThreadedVNCClientProxy は戻り値 None のメソッド呼び出しで
    後続呼び出しが壊れるため、keyEvent の直接利用は避け、
    常に self を返す keyDown/keyUp/keyPress を使う。
    """
    try:
        from vncdotool.client import KEYMAP
    except ImportError:
        return
    KEYMAP.setdefault("backspace", 0xFF08)
    KEYMAP.setdefault("escape", 0xFF1B)
    KEYMAP.setdefault("esc", 0xFF1B)
    KEYMAP.setdefault("insert", 0xFF63)
    KEYMAP.setdefault("page_up", 0xFF55)
    KEYMAP.setdefault("page_down", 0xFF56)
    KEYMAP.setdefault("print_screen", 0xFF61)


_ensure_vncdotool_keymap()


# USキーボードの Shift 合成表: シフト文字 → 素キー
# QEMU の VNC サーバは Shift 状態を復元しないため明示的に合成する。
_SHIFT_PAIRS: dict[str, str] = {
    "~": "`",
    "!": "1",
    "@": "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "^": "6",
    "&": "7",
    "*": "8",
    "(": "9",
    ")": "0",
    "_": "-",
    "+": "=",
    "{": "[",
    "}": "]",
    "|": "\\",
    ":": ";",
    '"': "'",
    "<": ",",
    ">": ".",
    "?": "/",
}
# 注意: 大文字 A-Z はここに入れないこと。QEMU が keysym から
# 直接大文字を復元するため、素通し keyPress で正しく届く。
# 手動 Shift 合成すると逆に小文字化する（実測）。


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
        # カーソル位置を内部追跡（vncdotool は取得APIを持たないため）
        self._cursor_x = 0
        self._cursor_y = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def cursor_x(self) -> int:
        return self._cursor_x

    @property
    def cursor_y(self) -> int:
        return self._cursor_y

    # ── 接続管理 ─────────────────────────────────────────

    def connect(self, host: str, port: int = 5900, password: str | None = None) -> None:
        """VNCサーバーに接続する。"""
        from vncdotool import api

        # vncdotool: double-colon for explicit port (single colon = display number)
        server = f"{host}::{port}"
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

    # ── 画面キャプチャ ────────────────────────────────────

    def capture_screen(self, *, with_overlay: bool = True) -> Screenshot:
        """画面全体をキャプチャする。

        with_overlay=True の場合、座標グリッド＋カーソル位置を重畳する。
        取得した実画像サイズを _width/_height に記録し、座標クランプに使う。
        """
        self._ensure_connected()

        buf = io.BytesIO()
        self._client.captureScreen(buf, format="png")
        data = buf.getvalue()
        self._frame_count += 1

        width, height = self._width or 1024, self._height or 768
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as img:
                width, height = img.size
        except Exception:
            logger.debug("スクリーンショットサイズ取得に失敗、デフォルトを使用")
        self._width, self._height = width, height

        ss = Screenshot(
            image_bytes=data,
            width=width,
            height=height,
            timestamp=time.monotonic(),
            frame_number=self._frame_count,
        )

        if with_overlay:
            return ss.with_overlay(
                cursor_x=self._cursor_x,
                cursor_y=self._cursor_y,
            )
        return ss

    def capture_raw(self) -> Screenshot:
        """オーバーレイなしの生スクリーンショットを取得する。"""
        return self.capture_screen(with_overlay=False)

    def capture_region(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> Screenshot:
        """指定領域をキャプチャする（オーバーレイなし）。"""
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

    # ── マウス操作（カーソル追跡つき） ────────────────────

    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        """座標を画面内にクランプする。サイズ不明時は0以上に丸める。"""
        if self._width > 0 and self._height > 0:
            return (
                max(0, min(int(x), self._width - 1)),
                max(0, min(int(y), self._height - 1)),
            )
        return (max(0, int(x)), max(0, int(y)))

    def mouse_move(self, x: int, y: int) -> None:
        self._ensure_connected()
        x, y = self._clamp(x, y)
        self._client.mouseMove(x, y)
        self._cursor_x = x
        self._cursor_y = y

    def mouse_down(self, button: int = 1) -> None:
        self._ensure_connected()
        self._client.mouseDown(button)

    def mouse_up(self, button: int = 1) -> None:
        self._ensure_connected()
        self._client.mouseUp(button)

    def mouse_click(self, x: int | None = None, y: int | None = None, button: int = 1) -> None:
        """指定座標をクリック（move → 整定 → press → 50ms → release）。

        move 直後に少し待つことで、VM側がカーソル移動を確定してから
        クリックを受け付けられるようにする。
        """
        if x is not None and y is not None:
            self.mouse_move(x, y)
            time.sleep(0.1)  # 移動の整定待ち（VM側の描画・フォーカス確定）
        self.mouse_down(button)
        time.sleep(0.05)  # 押下時間を確保（短すぎるとOSがクリックを認識しない）
        self.mouse_up(button)

    def mouse_double_click(
        self, x: int | None = None, y: int | None = None, button: int = 1
    ) -> None:
        """ダブルクリック。クリック間に100msの間隔を入れる。"""
        if x is not None and y is not None:
            x, y = self._clamp(x, y)
        self.mouse_click(x, y, button)
        time.sleep(0.1)  # OSのダブルクリック検出間隔（50ms押下 + 100ms間隔 = OS仕様内）
        # 2発目は同座標に明示移動してから（現在位置撃ちのブレを防ぐ）
        if x is not None and y is not None:
            self.mouse_click(x, y, button)
        else:
            self.mouse_click(self._cursor_x, self._cursor_y, button)

    def mouse_drag(
        self, start_x: int, start_y: int, end_x: int, end_y: int, button: int = 1
    ) -> None:
        """ドラッグ操作。距離に応じてステップ数を調整する。

        vncdotool の mouseDrag() は戻り値 None のためプロキシ連鎖を壊す。
        代わりに mouseMove の連打で自前ステップする（全て chain-safe）。
        """
        self._ensure_connected()
        start_x, start_y = self._clamp(start_x, start_y)
        end_x, end_y = self._clamp(end_x, end_y)
        dist = ((end_x - start_x) ** 2 + (end_y - start_y) ** 2) ** 0.5
        # 短距離は細かく、長距離も最低20ステップで滑らかに
        steps = max(2, min(50, max(20, int(dist / 10))))
        self._client.mouseMove(start_x, start_y)
        time.sleep(0.1)
        self._client.mouseDown(button)
        time.sleep(0.05)
        for i in range(1, steps + 1):
            x = int(start_x + (end_x - start_x) * i / steps)
            y = int(start_y + (end_y - start_y) * i / steps)
            self._client.mouseMove(x, y)
            time.sleep(0.015)
        self._client.mouseUp(button)
        self._cursor_x = end_x
        self._cursor_y = end_y

    def mouse_scroll(self, direction: str, amount: int = 1) -> None:
        """スクロール。"""
        self._ensure_connected()
        btn = _BUTTON_SCROLL_UP if direction == "up" else _BUTTON_SCROLL_DOWN
        for _ in range(abs(amount)):
            self._client.mouseDown(btn)
            self._client.mouseUp(btn)

    # ── キーボード操作 ────────────────────────────────────
    # keyDown/keyUp/keyPress（self を返す）を使い、戻り値 None の
    # keyEvent 直接呼び出しは避ける（プロキシ連鎖が壊れ、次回呼び出しで
    # 落ちるため。mouseDrag も同様の理由で使わず自前ステップする）。

    def key_press(self, key: str) -> None:
        """キーを押して離す（50ms押下）。"""
        self._ensure_connected()
        mapped = self._map_key(key)
        self._client.keyDown(mapped)
        time.sleep(0.05)  # 押下時間を確保
        self._client.keyUp(mapped)

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
        """キーコンビネーション（Ctrl+C 等）。各押下間に20msを挟む。"""
        self._ensure_connected()
        mapped = [self._map_key(k) for k in keys]
        # すべて押す
        for k in mapped:
            self._client.keyDown(k)
            time.sleep(0.02)
        time.sleep(0.05)
        # 逆順で離す
        for k in reversed(mapped):
            self._client.keyUp(k)
            time.sleep(0.02)

    def type_text(self, text: str) -> None:
        """テキストを1文字ずつキーイベントで送信する。

        注意: vncdotool の paste() はクリップボード経由だが、QEMU 標準の
        VNC サーバは ClientCutText をゲストに注入しないため無視される。
        そのため ASCII 文字は keyPress で1文字ずつ送る。
        さらに QEMU は Shift 合成を復元しないため、`>` や `"` 等の
        Shift 文字は Shift押下＋素キーの2段階で送る。
        非ASCII文字は QEMU が変換できない場合があり、その場合は送らない。
        """
        self._ensure_connected()
        sent = 0
        for ch in text:
            if ch == "\n":
                self.key_press("enter")
                sent += 1
            elif ch == "\t":
                self.key_press("tab")
                sent += 1
            elif ch in _SHIFT_PAIRS:
                self.key_down("shift")
                self._client.keyPress(_SHIFT_PAIRS[ch])
                self.key_up("shift")
                time.sleep(0.01)
                sent += 1
            elif ch.isascii() and ch.isprintable():
                self._client.keyPress(ch)
                time.sleep(0.01)
                sent += 1
            else:
                logger.warning("送信不可の文字をスキップ: U+%04X", ord(ch))
        logger.debug("テキスト送信: %d/%d 文字", sent, len(text))

    # ── 内部 ──────────────────────────────────────────────

    def _ensure_connected(self) -> None:
        if not self._connected or self._client is None:
            raise RuntimeError("VNCに接続されていません")

    @staticmethod
    def _map_key(key: str) -> str:
        """キー名をvncdotool形式に変換（KEYMAPは起動時に補完済み）。"""
        return _KEY_MAP.get(key.lower(), key.lower())
