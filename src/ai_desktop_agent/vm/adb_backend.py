"""ADB表示バックエンド — Android端末（Redroid等）を操作する。

VNCの代わりにAndroid Debug Bridgeを使う:
- 画面取得: `adb exec-out screencap -p`（PNG）
- タップ/スワイプ: `adb shell input tap/swipe`
- キー/文字: `adb shell input keyevent/text`

`DisplayBackend` 互換のため、既存の `ActionExecutor`・`TaskSession` が
そのまま使える（`left_click`→タップ、`drag`→スワイプに写像される）。
タッチデバイスにホバー概念はないため `mouse_move` は座標追跡のみ行う。

制限（v1）:
- ライブ視聴（noVNC相当）は未対応。LLM用の画面取得のみ。
- 日本語入力は未対応（`adb shell input text` がASCIIのみ）。
- `key_down` / `key_up`（長押し・コンボ）は未対応。
"""

from __future__ import annotations

import io
import logging
import subprocess
import time
from collections.abc import Callable, Sequence

from ai_desktop_agent.vm.base import DisplayBackend
from ai_desktop_agent.vm.screenshot import Screenshot

logger = logging.getLogger(__name__)

#: デフォルトのADBポート（コンテナ内）
DEFAULT_ADB_PORT = 5555
#: スワイプの既定duration（ms）
SWIPE_DURATION_MS = 300
#: `input text` で使えない文字（ASCII外は事前に弾く）
#: https://developer.android.com/tools/adb#input-textや実装上の制約による
_KEYCODE_MAP: dict[str, str] = {
    "enter": "KEYCODE_ENTER",
    "tab": "KEYCODE_TAB",
    "space": "KEYCODE_SPACE",
    "escape": "KEYCODE_BACK",
    "backspace": "KEYCODE_DEL",
    "delete": "KEYCODE_FORWARD_DEL",
    "home": "KEYCODE_HOME",
    "back": "KEYCODE_BACK",
    "up": "KEYCODE_DPAD_UP",
    "down": "KEYCODE_DPAD_DOWN",
    "left": "KEYCODE_DPAD_LEFT",
    "right": "KEYCODE_DPAD_RIGHT",
    "menu": "KEYCODE_MENU",
    "wake": "KEYCODE_WAKEUP",
    "sleep": "KEYCODE_SLEEP",
}


def _default_runner(
    args: Sequence[str], *, timeout: float = 30.0, binary: bool = False
) -> subprocess.CompletedProcess:
    """`adb` を実行する（テスト時は差し替え可能）。"""
    return subprocess.run(
        list(args),
        capture_output=True,
        timeout=timeout,
        check=False,
        text=not binary,
    )


# `adb shell input text` で特別扱いが必要な文字
_INPUT_TEXT_SPECIALS = set(" \"'()<>|;&*?~$!#`[]=^{}\\//@:,")


def _escape_input_text(text: str) -> str:
    """`adb shell input text` 用にエスケープする。空白は %s 化する。"""
    out = []
    for ch in text:
        if ch == " ":
            out.append("%s")
        elif ch in _INPUT_TEXT_SPECIALS:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


class AdbBackend(DisplayBackend):
    """ADB経由のAndroid表示バックエンド。

    Args:
        serial: `adb -s` に渡すシリアル（`host:port` 形式）。
            省略時は `connect()` で `host:port` から組み立てる。
        adb: adb実行ファイルのパス。
        runner: コマンド実行関数（テスト用の差し替え点）。
        default_width: 画面幅が取れない場合のフォールバック。
        default_height: 画面高さが取れない場合のフォールバック。
    """

    def __init__(
        self,
        serial: str | None = None,
        adb: str = "adb",
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        default_width: int = 1080,
        default_height: int = 1920,
    ) -> None:
        self._serial = serial
        self._adb = adb
        self._run = runner or _default_runner
        self._connected = False
        self._cursor_x = 0
        self._cursor_y = 0
        self._width = default_width
        self._height = default_height
        self._frame_count = 0

    # ── 接続管理 ──────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def serial(self) -> str | None:
        """`adb -s` 用シリアル。未接続時は None。"""
        return self._serial

    def connect(self, host: str, port: int = DEFAULT_ADB_PORT, password: str | None = None) -> None:
        """ADB over TCP で接続する。`password` は使わない（互換用）。"""
        del password
        serial = f"{host}:{port}"
        logger.info("ADB接続中: %s", serial)
        cp = self._run([self._adb, "connect", serial], timeout=15.0)
        if cp.returncode != 0:
            raise RuntimeError(f"adb connect に失敗: {serial}: {cp.stderr or cp.stdout}")
        self._serial = serial
        self._require_device()
        self._connected = True
        logger.info("ADB接続完了: %s", serial)

    def _require_device(self) -> None:
        """デバイスが見えるまで待つ。"""
        cp = self._run([self._adb, "-s", str(self._serial), "wait-for-device"], timeout=60.0)
        if cp.returncode != 0:
            raise RuntimeError(f"Androidデバイスが見えません: {self._serial}")

    def disconnect(self) -> None:
        if self._serial is not None:
            try:
                self._run([self._adb, "disconnect", self._serial], timeout=10.0)
            except Exception:
                logger.debug("adb disconnect に失敗", exc_info=True)
        self._serial = None
        self._connected = False

    def wait_boot(self, timeout: float = 180.0) -> None:
        """`sys.boot_completed=1` になるまで待つ（起動直後用）。"""
        self._ensure_connected()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = self._shell("getprop sys.boot_completed", timeout=10.0).strip()
            if out == "1":
                return
            time.sleep(5.0)
        raise TimeoutError(f"Androidの起動が完了しません: {self._serial}")

    # ── 画面キャプチャ ──────────────────────────────────

    def capture_screen(self) -> Screenshot:
        """`screencap` で画面全体を取得し、座標グリッドを重畳する。"""
        raw = self.capture_raw()
        return raw.with_overlay(cursor_x=self._cursor_x, cursor_y=self._cursor_y)

    def capture_raw(self) -> Screenshot:
        """オーバーレイなしの生スクリーンショットを取得する。"""
        self._ensure_connected()
        cp = self._run(
            [self._adb, "-s", str(self._serial), "exec-out", "screencap -p"],
            timeout=30.0,
            binary=True,
        )
        if cp.returncode != 0 or not cp.stdout:
            raise RuntimeError(f"screencap に失敗: {self._serial}")
        data = bytes(cp.stdout)
        width, height = self._width, self._height
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as img:
                width, height = img.size
        except Exception:
            logger.debug("スクリーンショットサイズ取得に失敗、デフォルトを使用")
        self._width, self._height = width, height
        self._frame_count += 1
        return Screenshot(
            image_bytes=data,
            width=width,
            height=height,
            timestamp=time.monotonic(),
            frame_number=self._frame_count,
        )

    def capture_region(self, x: int, y: int, width: int, height: int) -> Screenshot:
        """指定領域をキャプチャする（全体取得＋切り出し）。"""
        self._ensure_connected()
        raw = self.capture_raw()
        zoomed, _meta = raw.crop_with_meta(x, y, width, height)
        return zoomed

    # ── タッチ操作（マウス互換） ─────────────────────────

    def mouse_move(self, x: int, y: int) -> None:
        """タッチデバイスにホバーはないため座標追跡のみ行う。"""
        self._cursor_x, self._cursor_y = int(x), int(y)

    def mouse_down(self, button: int = 1) -> None:
        del button  # タッチにボタン概念なし（tap/swipe側で処理）

    def mouse_up(self, button: int = 1) -> None:
        del button

    def mouse_click(self, x: int | None = None, y: int | None = None, button: int = 1) -> None:
        """タップとして送信する。"""
        del button
        if x is None or y is None:
            raise ValueError("Android操作は座標必須です")
        self.mouse_move(x, y)
        self._shell(f"input tap {int(x)} {int(y)}")

    def mouse_double_click(
        self, x: int | None = None, y: int | None = None, button: int = 1
    ) -> None:
        self.mouse_click(x, y, button)
        self.mouse_click(x, y, button)

    def mouse_drag(
        self, start_x: int, start_y: int, end_x: int, end_y: int, button: int = 1
    ) -> None:
        """スワイプとして送信する。"""
        del button
        self.mouse_move(end_x, end_y)
        self._shell(
            f"input swipe {int(start_x)} {int(start_y)} {int(end_x)} {int(end_y)}"
            f" {SWIPE_DURATION_MS}"
        )

    def mouse_scroll(self, direction: str, amount: int) -> None:
        """画面中央のスワイプでスクロールする。"""
        if direction not in ("up", "down"):
            raise ValueError(f"direction は up/down: {direction}")
        cx, h = self._width // 2, self._height
        y1, y2 = (h * 2 // 3, h // 3) if direction == "up" else (h // 3, h * 2 // 3)
        for _ in range(max(1, int(amount))):
            self._shell(f"input swipe {cx} {y1} {cx} {y2} {SWIPE_DURATION_MS}")

    # ── キーボード操作 ──────────────────────────────────

    def key_press(self, key: str) -> None:
        """キー名→keyeventに変換して送信する。1文字はテキスト入力。"""
        code = _KEYCODE_MAP.get(key.lower())
        if code is not None:
            self._shell(f"input keyevent {code}")
            return
        if len(key) == 1:
            self.type_text(key)
            return
        raise ValueError(f"未対応のキー: {key}")

    def key_down(self, key: str) -> None:
        raise NotImplementedError(f"Androidで長押しは未対応です: {key}")

    def key_up(self, key: str) -> None:
        raise NotImplementedError(f"Androidで長押しは未対応です: {key}")

    def type_text(self, text: str) -> None:
        """ASCIIテキストを入力する。日本語は未対応のため事前に弾く。"""
        if any(ord(ch) > 127 for ch in text):
            raise ValueError("Androidへの日本語入力は未対応です（ASCIIのみ）")
        self._shell(f"input text {_escape_input_text(text)}")

    # ── 内部 ────────────────────────────────────────────

    def _ensure_connected(self) -> None:
        if not self._connected or self._serial is None:
            raise RuntimeError("Androidに接続されていません")

    def _shell(self, command: str, timeout: float = 30.0) -> str:
        """`adb shell <command>` を実行し、標準出力を返す。"""
        self._ensure_connected()
        cp = self._run([self._adb, "-s", str(self._serial), "shell", command], timeout=timeout)
        if cp.returncode != 0:
            raise RuntimeError(f"adb shell に失敗: {command}: {cp.stderr or cp.stdout}")
        out = cp.stdout
        return out.decode() if isinstance(out, bytes) else (out or "")
