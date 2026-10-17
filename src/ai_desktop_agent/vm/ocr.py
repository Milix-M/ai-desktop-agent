"""OCR — 画面テキスト抽出（tesseract）。

スクリーンショット以外の情報源として、画面上の文字をテキストで渡す。
executor の text_extractor と LLM プロンプトの両方で使う。
"""

from __future__ import annotations

import hashlib
import io
import logging
from collections import OrderedDict

logger = logging.getLogger(__name__)

_CACHE_SIZE = 8
_cache: OrderedDict[str, str] = OrderedDict()


def _available() -> bool:
    try:
        from shutil import which

        import pytesseract  # noqa: F401

        return which("tesseract") is not None
    except ImportError:
        return False


def extract_text(image_bytes: bytes, *, lang: str = "eng", max_chars: int = 2000) -> str:
    """画像からテキストを抽出する。失敗時は空文字列。

    同一画像の再計算を避けるため小規模キャッシュを持つ。
    """
    key = hashlib.sha256(image_bytes).hexdigest()
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]

    text = ""
    try:
        import pytesseract
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            # 小さい画像のみ2倍拡大する（大きい画像の拡大は遅い割に効果薄）
            w, h = img.size
            if max(w, h) < 800:
                img = img.resize((w * 2, h * 2))
            text = pytesseract.image_to_string(img.convert("L"), lang=lang) or ""
            text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    except Exception:
        logger.debug("OCRに失敗", exc_info=True)
        text = ""
    text = text[:max_chars]
    _cache[key] = text
    if len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return text


def is_available() -> bool:
    """OCRが利用可能かどうか。"""
    return _available()


def extract_words(image_bytes: bytes, *, lang: str = "eng", min_conf: float = 30.0) -> list[dict]:
    """画像から単語ごとの座標付きテキストを抽出する。

    機械的プロビジョニング用に、ボタン等のタップ位置を特定する。
    PSM 6（等間隔ブロック）を使い、UIスクリーンショットでの検出率を上げる。
    戻り値は ``{"text", "x", "y", "w", "h", "conf"}`` の列
    （x, y は左上、中心は x+w//2, y+h//2）。失敗時は []。
    """
    words: list[dict] = []
    try:
        import pytesseract
        from PIL import Image
        from pytesseract import Output

        with Image.open(io.BytesIO(image_bytes)) as img:
            data = pytesseract.image_to_data(
                img.convert("L"), lang=lang, config="--psm 6", output_type=Output.DICT
            )
        n = len(data.get("text", []))
        for i in range(n):
            text = (data["text"][i] or "").strip()
            if not text:
                continue
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError):
                continue
            if conf < min_conf:
                continue
            try:
                x, y, w, h = (
                    int(data["left"][i]),
                    int(data["top"][i]),
                    int(data["width"][i]),
                    int(data["height"][i]),
                )
            except (TypeError, ValueError):
                continue
            if w <= 0 or h <= 0:
                continue
            words.append({"text": text, "x": x, "y": y, "w": w, "h": h, "conf": conf})
    except Exception:
        logger.debug("OCR単語抽出に失敗", exc_info=True)
    return words


def find_word(
    image_bytes: bytes, word: str, *, lang: str = "eng", min_conf: float = 30.0
) -> tuple[int, int] | None:
    """指定語の中心座標を返す（大文字小文字を区別しない）。なければ None。"""
    for w in extract_words(image_bytes, lang=lang, min_conf=min_conf):
        if w["text"].lower() == word.lower():
            return (w["x"] + w["w"] // 2, w["y"] + w["h"] // 2)
    return None
