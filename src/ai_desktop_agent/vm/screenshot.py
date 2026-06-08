"""画面キャプチャの値オブジェクト。

オーバーレイや領域切り出しのユーティリティメソッドを持つ。
"""

from __future__ import annotations

import dataclasses
import io

from PIL import Image


@dataclasses.dataclass(frozen=True)
class Screenshot:
    """単一の画面キャプチャ。"""

    image_bytes: bytes  # PNG 画像バイナリ
    width: int  # 画像の幅（px）
    height: int  # 画像の高さ（px）
    timestamp: float = 0.0
    frame_number: int = 0

    # ── ユーティリティ ─────────────────────────────────────

    def with_overlay(
        self,
        cursor_x: int | None = None,
        cursor_y: int | None = None,
        *,
        show_grid: bool = True,
    ) -> Screenshot:
        """座標グリッド＋カーソル位置を重畳したスクリーンショットを返す。"""
        from ai_desktop_agent.vm.overlay import add_coordinate_overlay

        return add_coordinate_overlay(
            self,
            cursor_x=cursor_x,
            cursor_y=cursor_y,
            show_grid=show_grid,
        )

    def crop(
        self,
        x: int,
        y: int,
        region_w: int,
        region_h: int,
        *,
        scale: float = 2.0,
        max_dim: int = 1024,
    ) -> Screenshot:
        """指定領域を切り出し＋拡大したスクリーンショットを返す。"""
        from ai_desktop_agent.vm.overlay import crop_region

        return crop_region(self, x, y, region_w, region_h, scale=scale, max_dim=max_dim)

    def to_pil(self) -> Image.Image:
        """PIL Image に変換する。"""
        return Image.open(io.BytesIO(self.image_bytes))

    @property
    def size_bytes(self) -> int:
        """画像データのバイト数。"""
        return len(self.image_bytes)

    @property
    def image_base64(self) -> str:
        """base64 エンコードされた画像データ（data: URL用）。"""
        import base64

        return base64.b64encode(self.image_bytes).decode("ascii")

    @property
    def image_data_url(self) -> str:
        """data: URL 形式の画像。"""
        return f"data:image/png;base64,{self.image_base64}"
