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
