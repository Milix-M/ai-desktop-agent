"""テスト用フェイク表示バックエンド。

実際のVM/VNCを使わず、操作を記録して検証可能にする。
"""

import io

from PIL import Image

from ai_desktop_agent.vm.base import DisplayBackend
from ai_desktop_agent.vm.screenshot import Screenshot


def _make_dummy_png() -> bytes:
    """1x1 ピクセルの最小PNGを生成。"""
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), color="black").save(buf, format="PNG")
    return buf.getvalue()


class FakeDisplayBackend(DisplayBackend):
    """テスト用のフェイクバックエンド。

    すべての操作を記録し、実際のVMなしでテストできる。
    """

    def __init__(self, screen_width: int = 1024, screen_height: int = 768) -> None:
        self._connected = True  # テストではデフォルトで接続状態
        self._screen_width = screen_width
        self._screen_height = screen_height
        self._frame_count = 0

        # 操作ログ
        self.mouse_moves: list[tuple[int, int]] = []
        self.clicks: list[dict] = []  # {button, x, y}
        self.drags: list[dict] = []  # {start, end}
        self.scrolls: list[dict] = []  # {direction, amount}
        self.key_presses: list[str] = []
        self.key_combos: list[list[str]] = []
        self.text_inputs: list[str] = []
        self.screenshots_taken: int = 0
        self.connections: list[str] = []

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self, host: str, port: int = 5900, password: str | None = None) -> None:
        self._connected = True
        self.connections.append(f"{host}:{port}")

    def disconnect(self) -> None:
        self._connected = False

    def capture_screen(self, *, with_overlay: bool = True) -> Screenshot:
        del with_overlay  # Fakeは常に同一ダミー画像
        self._frame_count += 1
        self.screenshots_taken += 1
        return Screenshot(
            image_bytes=_make_dummy_png(),
            width=self._screen_width,
            height=self._screen_height,
            timestamp=0.0,
            frame_number=self._frame_count,
        )

    def capture_raw(self) -> Screenshot:
        """オーバーレイなしの生スクリーンショット（Fakeは同一画像）。"""
        return self.capture_screen(with_overlay=False)

    def capture_region(self, x: int, y: int, width: int, height: int) -> Screenshot:
        self._frame_count += 1
        self.screenshots_taken += 1
        return Screenshot(
            image_bytes=_make_dummy_png(),
            width=width,
            height=height,
            timestamp=0.0,
            frame_number=self._frame_count,
        )

    @property
    def screen_width(self) -> int:
        return self._screen_width

    @property
    def screen_height(self) -> int:
        return self._screen_height

    def mouse_move(self, x: int, y: int) -> None:
        self.mouse_moves.append((x, y))

    def mouse_down(self, button: int = 1) -> None:
        pass  # mouse_click でまとめて記録

    def mouse_up(self, button: int = 1) -> None:
        pass

    def mouse_click(self, x: int | None = None, y: int | None = None, button: int = 1) -> None:
        # None の場合は (0, 0) 扱い（旧「現在位置クリック」の代替。座標必須化後は通常呼ばれない）
        cx, cy = (x if x is not None else 0, y if y is not None else 0)
        self.mouse_moves.append((cx, cy))
        self.clicks.append({"button": button, "x": cx, "y": cy})

    def mouse_double_click(
        self, x: int | None = None, y: int | None = None, button: int = 1
    ) -> None:
        cx, cy = (x if x is not None else 0, y if y is not None else 0)
        self.mouse_moves.append((cx, cy))
        self.clicks.append({"button": button, "x": cx, "y": cy, "double": True})

    def mouse_drag(
        self, start_x: int, start_y: int, end_x: int, end_y: int, button: int = 1
    ) -> None:
        self.mouse_moves.append((start_x, start_y))
        self.mouse_moves.append((end_x, end_y))
        self.drags.append(
            {
                "start": (start_x, start_y),
                "end": (end_x, end_y),
                "button": button,
            }
        )

    def mouse_scroll(self, direction: str, amount: int) -> None:
        self.scrolls.append({"direction": direction, "amount": amount})

    def key_press(self, key: str) -> None:
        self.key_presses.append(key)

    def key_down(self, key: str) -> None:
        self.key_presses.append(f"down:{key}")

    def key_up(self, key: str) -> None:
        self.key_presses.append(f"up:{key}")

    def key_combo(self, keys: list[str]) -> None:
        self.key_combos.append(keys)

    def type_text(self, text: str) -> None:
        self.text_inputs.append(text)

    # ── テスト用ヘルパー ───────────────────────────────

    @property
    def total_actions(self) -> int:
        """記録された全操作の数。"""
        return (
            len(self.clicks)
            + len(self.drags)
            + len(self.scrolls)
            + len(self.key_presses)
            + len(self.key_combos)
            + len(self.text_inputs)
            + self.screenshots_taken
        )

    def reset_logs(self) -> None:
        """操作ログをリセット。"""
        self.mouse_moves.clear()
        self.clicks.clear()
        self.drags.clear()
        self.scrolls.clear()
        self.key_presses.clear()
        self.key_combos.clear()
        self.text_inputs.clear()
        self.screenshots_taken = 0
        self._frame_count = 0
