"""スクリーンショット型 — VMの画面キャプチャを表現する。

VNCから取得した画像データをラップし、メタデータを付与する。
"""

from dataclasses import dataclass


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
