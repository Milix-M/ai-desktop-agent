"""テンプレート照合 — 画面上の既知UI部品を座標特定する。

LLMの推測に頼らず、ボタン等の小物を確定的に捉えるための土台。
PILのみで動作し、追加依存は不要。
"""

from __future__ import annotations

import io
import logging

from PIL import Image

logger = logging.getLogger(__name__)


def find_template(
    screen_bytes: bytes,
    template_bytes: bytes,
    *,
    threshold: float = 0.9,
    max_stride: int = 4,
) -> tuple[int, int, float] | None:
    """画面内からテンプレートに最も似た位置を探す。

    正規化相互相関（NCC）をグレースケールで計算する。
    計算量削減のため max_stride 間引きで走査する。

    Args:
        screen_bytes: 画面全体のPNG。
        template_bytes: 探したい部品のPNG。
        threshold: 採用する最低スコア（0.0〜1.0）。
        max_stride: 走査の間引き幅（大きいほど高速・粗い）。

    Returns:
        (中心x, 中心y, スコア) または見つからなければ None。
    """
    screen = Image.open(io.BytesIO(screen_bytes)).convert("L")
    template = Image.open(io.BytesIO(template_bytes)).convert("L")
    sw, sh = screen.size
    tw, th = template.size
    if tw > sw or th > sh or tw == 0 or th == 0:
        return None

    t_px = template.load()
    s_px = screen.load()

    def ncc_at(left: int, top: int, stride: int) -> float | None:
        n = 0
        s_sum = 0.0
        s_sq = 0.0
        t_sum = 0.0
        t_sq = 0.0
        cross = 0.0
        sad = 0.0
        for y in range(0, th, stride):
            for x in range(0, tw, stride):
                s = s_px[left + x, top + y]
                t = t_px[x, y]
                s_sum += s
                s_sq += s * s
                t_sum += t
                t_sq += t * t
                cross += s * t
                sad += abs(s - t)
                n += 1
        if n == 0:
            return None
        s_mean = s_sum / n
        t_mean = t_sum / n
        denom = ((s_sq - n * s_mean * s_mean) * (t_sq - n * t_mean * t_mean)) ** 0.5
        if denom <= 0:
            # 単色テンプレート等：平均差分で評価する
            return max(0.0, 1.0 - (sad / n) / 255.0)
        return max(-1.0, min(1.0, (cross - n * s_mean * t_mean) / denom))

    best: tuple[float, int, int] | None = None  # (score, cx, cy)
    best_pos: tuple[int, int] | None = None
    strong_count = 0  # 閾値超えの候補数（多すぎたら曖昧として不採用）

    # 粗走査
    for top in range(0, sh - th + 1, max_stride):
        for left in range(0, sw - tw + 1, max_stride):
            score = ncc_at(left, top, max_stride)
            if score is None:
                continue
            if score >= threshold:
                strong_count += 1
            if best is None or score > best[0]:
                best = (score, left + tw // 2, top + th // 2)
                best_pos = (left, top)

    if best is None or best_pos is None:
        return None
    if strong_count > 10:
        logger.debug("候補が多すぎるため曖昧として不採用: %d", strong_count)
        return None

    # 精密化（粗走査の最良点周辺を stride 1 で再走査）
    bl, bt = best_pos
    for top in range(max(0, bt - max_stride), min(sh - th, bt + max_stride) + 1):
        for left in range(max(0, bl - max_stride), min(sw - tw, bl + max_stride) + 1):
            score = ncc_at(left, top, 1)
            if score is None:
                continue
            if score > best[0]:
                best = (score, left + tw // 2, top + th // 2)

    if best[0] < threshold:
        return None
    return (best[1], best[2], best[0])
