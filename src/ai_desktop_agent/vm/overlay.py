"""
スクリーンショットに視覚的な座標ヒントを重畳するモジュール。

LLMが画像から正確な座標を推測できない問題に対処するため、
グリッド線・座標マーカー・カーソル位置などを描画する。
"""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    from ai_desktop_agent.vm.screenshot import Screenshot

logger = logging.getLogger(__name__)

# ── 描画設定 ──────────────────────────────────────────────
GRID_COLOR = (255, 0, 0, 60)  # 赤・半透明のグリッド線
GRID_MAJOR_COLOR = (255, 0, 0, 120)  # 太線（100px ごと）
MARKER_COLOR = (255, 50, 50, 200)  # 座標マーカー文字色
CURSOR_COLOR = (0, 255, 0, 220)  # カーソル位置の色
CURSOR_SIZE = 20  # カーソル十字の長さ
GRID_SPACING = 50  # グリッド間隔（px）
MAJOR_GRID_EVERY = 2  # 何本ごとに太線にするか（50px x 2 = 100px）


def add_coordinate_overlay(
    screenshot: Screenshot,
    cursor_x: int | None = None,
    cursor_y: int | None = None,
    *,
    grid_spacing: int = GRID_SPACING,
    show_grid: bool = True,
) -> Screenshot:
    """スクリーンショットに座標グリッド＋マーカー＋カーソルを重畳する。

    Args:
        screenshot: 元のスクリーンショット。
        cursor_x, cursor_y: 現在のマウスカーソル位置（わかれば）。
        grid_spacing: グリッド線の間隔（px）。
        show_grid: False ならグリッド線を描かない（マーカーのみ）。

    Returns:
        重畳後の Screenshot（新しいインスタンス）。
    """
    from ai_desktop_agent.vm.screenshot import Screenshot

    img = Image.open(io.BytesIO(screenshot.image_bytes)).convert("RGBA")
    w, h = img.size
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # ── フォント（できるだけ小さい等幅） ──
    font = _load_font(size=10)

    # ── グリッド線 ──
    if show_grid:
        for i, x in enumerate(range(0, w, grid_spacing)):
            is_major = (i % MAJOR_GRID_EVERY) == 0
            color = GRID_MAJOR_COLOR if is_major else GRID_COLOR
            draw.line([(x, 0), (x, h)], fill=color, width=1)

        for i, y in enumerate(range(0, h, grid_spacing)):
            is_major = (i % MAJOR_GRID_EVERY) == 0
            color = GRID_MAJOR_COLOR if is_major else GRID_COLOR
            draw.line([(0, y), (w, y)], fill=color, width=1)

    # ── 端の座標マーカー（上辺・左辺） ──
    for x in range(0, w, grid_spacing * MAJOR_GRID_EVERY):
        draw.text((x + 3, 2), str(x), fill=MARKER_COLOR, font=font)
    for y in range(0, h, grid_spacing * MAJOR_GRID_EVERY):
        draw.text((2, y + 1), str(y), fill=MARKER_COLOR, font=font)

    # ── 四隅の太い枠 ──
    draw.rectangle([(0, 0), (w - 1, h - 1)], outline=(255, 0, 0, 180), width=2)

    # ── カーソル十字 ──
    if cursor_x is not None and cursor_y is not None:
        cx, cy = cursor_x, cursor_y
        s = CURSOR_SIZE
        # 水平線
        draw.line([(cx - s, cy), (cx + s, cy)], fill=CURSOR_COLOR, width=2)
        # 垂直線
        draw.line([(cx, cy - s), (cx, cy + s)], fill=CURSOR_COLOR, width=2)
        # 中心ドット
        draw.ellipse([(cx - 3, cy - 3), (cx + 3, cy + 3)], fill=CURSOR_COLOR)

    # ── 合成 ──
    img = Image.alpha_composite(img, overlay).convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Screenshot(
        image_bytes=buf.getvalue(),
        width=w,
        height=h,
        timestamp=screenshot.timestamp,
        frame_number=screenshot.frame_number,
    )


def crop_region(
    screenshot: Screenshot,
    x: int,
    y: int,
    region_w: int,
    region_h: int,
    *,
    scale: float = 2.0,
    max_dim: int = 1024,
) -> Screenshot:
    """指定領域を切り出して拡大する（ズームイン用）。

    Args:
        screenshot: 元のスクリーンショット。
        x, y: 領域の左上座標。
        region_w, region_h: 領域の幅・高さ。
        scale: 拡大率。
        max_dim: 拡大後の最大サイズ（長辺がこれを超えたら縮小）。

    Returns:
        切り出し＋拡大後の Screenshot。
    """
    from ai_desktop_agent.vm.screenshot import Screenshot

    img = Image.open(io.BytesIO(screenshot.image_bytes))
    w, h = img.size

    # クリップ
    x = max(0, min(x, w - 1))
    y = max(0, min(y, h - 1))
    rw = min(region_w, w - x)
    rh = min(region_h, h - y)

    cropped = img.crop((x, y, x + rw, y + rh))

    # 拡大
    new_w = int(rw * scale)
    new_h = int(rh * scale)

    # 長辺でリミット
    if max(new_w, new_h) > max_dim:
        ratio = max_dim / max(new_w, new_h)
        new_w = int(new_w * ratio)
        new_h = int(new_h * ratio)

    zoomed = cropped.resize((new_w, new_h), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    zoomed.save(buf, format="PNG")
    return Screenshot(
        image_bytes=buf.getvalue(),
        width=new_w,
        height=new_h,
        timestamp=screenshot.timestamp,
        frame_number=screenshot.frame_number,
    )


def _load_font(size: int = 10) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """利用可能な等幅フォントを探して返す。"""
    import sys

    # フォント候補（Linux）
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/truetype/ubuntu/UbuntuMono-R.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    ]
    # macOS
    if sys.platform == "darwin":
        candidates.insert(0, "/System/Library/Fonts/Menlo.ttc")

    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue

    # フォールバック（デフォルトフォント）
    return ImageFont.load_default()
