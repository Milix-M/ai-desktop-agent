"""スクリーンショット型 — VMの画面キャプチャを表現する。

VNCから取得した画像データをラップし、メタデータを付与する。
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Screenshot:
    """VMの画面キャプチャ。

    Attributes:
        image_bytes: PNG形式の画像データ。
        width: 画像の幅 (px)。
        height: 画像の高さ (px)。
        timestamp: 取得時刻（UNIX秒）。
        frame_number: シーケンス番号。
    """

    image_bytes: bytes
    width: int
    height: int
    timestamp: float = 0.0
    frame_number: int = 0

    @property
    def size(self) -> tuple[int, int]:
        """(width, height) タプル。"""
        return (self.width, self.height)

    @property
    def size_bytes(self) -> int:
        """画像データのバイト数。"""
        return len(self.image_bytes)


# テスト用の小さなPNG（1x1 赤ピクセル）
_MOCK_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def make_mock_screenshot(width: int = 1024, height: int = 768) -> Screenshot:
    """テスト用のモックスクリーンショットを生成。"""
    return Screenshot(
        image_bytes=_MOCK_PNG,
        width=width,
        height=height,
        timestamp=0.0,
        frame_number=0,
    )
