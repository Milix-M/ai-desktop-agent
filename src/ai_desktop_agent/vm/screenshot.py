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
        grid_spacing: int | None = None,
    ) -> Screenshot:
        """座標グリッド＋カーソル位置を重畳したスクリーンショットを返す。"""
        from ai_desktop_agent.vm.overlay import GRID_SPACING, add_coordinate_overlay

        return add_coordinate_overlay(
            self,
            cursor_x=cursor_x,
            cursor_y=cursor_y,
            grid_spacing=grid_spacing if grid_spacing is not None else GRID_SPACING,
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

    def crop_with_meta(
        self,
        x: int,
        y: int,
        region_w: int,
        region_h: int,
        *,
        scale: float = 2.0,
        max_dim: int = 1024,
    ) -> tuple[Screenshot, tuple[int, int, float]]:
        """領域切り出し＋実倍率つきで返す。

        Returns:
            (拡大後Screenshot, (実際に使われたx, y, 実倍率)) のタプル。
            x, y は画面内にクリップされた値。
        """
        from ai_desktop_agent.vm.overlay import crop_region_with_meta

        zoomed, actual_scale = crop_region_with_meta(
            self, x, y, region_w, region_h, scale=scale, max_dim=max_dim
        )
        # クリップ後の原点を再計算（overlay側と同じロジック）
        img_w, img_h = self.width, self.height
        try:
            import io as _io

            from PIL import Image as _PILImage

            _img = _PILImage.open(_io.BytesIO(self.image_bytes))
            img_w, img_h = _img.size
        except Exception:
            pass
        cx = max(0, min(x, img_w - 1))
        cy = max(0, min(y, img_h - 1))
        return zoomed, (cx, cy, actual_scale)

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
