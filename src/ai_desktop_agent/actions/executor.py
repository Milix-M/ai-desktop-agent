"""アクション実行者 — ActionをDisplayBackendの操作に変換して実行する。

エージェントループから Action を受け取り、
DisplayBackend（VNCクライアント等）を通じてVM上で実行する。
"""

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.vm.base import DisplayBackend

logger = logging.getLogger(__name__)

# テキスト抽出器の型: 画像バイナリ → 抽出テキスト
TextExtractor = Callable[[bytes], str]


def _default_text_extractor(_image_bytes: bytes) -> str:
    """デフォルトのテキスト抽出器。OCRが設定されていない場合は空文字列を返す。"""
    return ""


class ActionExecutor:
    """ActionをVM上で実行するエンジン。

    DisplayBackend に依存し、各ActionTypeを適切な操作に変換する。

    Args:
        backend: VM操作に使用する表示バックエンド。
        text_extractor: スクリーンショットからテキストを抽出する関数。
            デフォルトは空文字列を返す。OCRを使用する場合は
            pytesseract等を使った関数を注入する。
    """

    def __init__(
        self,
        backend: DisplayBackend,
        text_extractor: TextExtractor | None = None,
    ) -> None:
        self._backend = backend
        self._extract_text = text_extractor or _default_text_extractor

    async def execute(self, action: Action) -> bool:
        """1つのアクションを実行する。

        Returns:
            実行に成功したかどうか。
        """
        start = time.monotonic()
        try:
            await self._dispatch(action)
            duration = (time.monotonic() - start) * 1000
            logger.info("[OK] %s (%.0fms)", action.description, duration)
            return True
        except Exception as e:
            duration = (time.monotonic() - start) * 1000
            logger.error("[FAIL] %s: %s (%.0fms)", action.description, e, duration)
            return False

    async def _dispatch(self, action: Action) -> None:
        """ActionTypeに応じて適切なバックエンド操作を呼び出す。"""
        p = action.params
        match action.action_type:
            # ── マウス操作 ──
            case ActionType.MOUSE_MOVE:
                self._backend.mouse_move(p["x"], p["y"])
            case ActionType.LEFT_CLICK:
                x = p.get("x")
                y = p.get("y")
                if x is not None and y is not None:
                    self._backend.mouse_click(x, y, button=1)
                else:
                    self._backend.mouse_down(1)
                    self._backend.mouse_up(1)
            case ActionType.RIGHT_CLICK:
                x = p.get("x")
                y = p.get("y")
                if x is not None and y is not None:
                    self._backend.mouse_click(x, y, button=3)
                else:
                    self._backend.mouse_down(3)
                    self._backend.mouse_up(3)
            case ActionType.DOUBLE_CLICK:
                x = p.get("x")
                y = p.get("y")
                self._backend.mouse_double_click(x, y)
            case ActionType.MIDDLE_CLICK:
                x = p.get("x")
                y = p.get("y")
                if x is not None and y is not None:
                    self._backend.mouse_click(x, y, button=2)
                else:
                    self._backend.mouse_down(2)
                    self._backend.mouse_up(2)
            case ActionType.DRAG:
                self._backend.mouse_drag(
                    p["start_x"],
                    p["start_y"],
                    p["end_x"],
                    p["end_y"],
                )
            case ActionType.SCROLL:
                self._backend.mouse_scroll(p["direction"], p["amount"])

            # ── キーボード操作 ──
            case ActionType.TYPE:
                self._backend.type_text(p["text"])
            case ActionType.KEY_PRESS:
                self._backend.key_press(p["key"])
            case ActionType.KEY_COMBO:
                self._backend.key_combo(p["keys"])
            case ActionType.KEY_HOLD:
                self._backend.key_down(p["key"])
                await asyncio.sleep(p["duration_ms"] / 1000.0)
                self._backend.key_up(p["key"])

            # ── 待機 ──
            case ActionType.WAIT:
                await asyncio.sleep(p["seconds"])
            case ActionType.WAIT_FOR_TEXT:
                await self._wait_for_text(p["text"], p["timeout"])
            case ActionType.WAIT_FOR_STILL:
                await self._wait_for_still(p["timeout"])

            # ── 観測 ──
            case ActionType.SCREENSHOT:
                self._backend.capture_screen()

            # ── メタ ──
            case ActionType.SUBTASK_COMPLETE:
                pass  # 何もしない（状態機械へのシグナル）

    # ── 待機ロジック ───────────────────────────────────

    async def _wait_for_text(self, text: str, timeout: float) -> None:
        """画面に指定テキストが現れるまでスクリーンショットを取得して待機。

        テキスト抽出器（OCR）が設定されている場合は抽出テキストから検索する。
        設定されていない場合は画面変化を検出する。
        """
        elapsed = 0.0
        interval = 0.5
        prev_hash = ""

        while elapsed < timeout:
            ss = self._backend.capture_screen()
            extracted = self._extract_text(ss.image_bytes)

            if extracted and text in extracted:
                logger.info("テキスト検出: %s", text)
                return

            # OCR未設定時: 画面変化があれば何か表示されたとみなす
            current_hash = self._image_hash(ss.image_bytes)
            if prev_hash and current_hash != prev_hash and not self._extract_text(ss.image_bytes):
                # OCRがない → 変化を検出したので進む
                logger.debug("画面変化を検出 (wait_for_text fallback)")
                return

            prev_hash = current_hash
            await asyncio.sleep(interval)
            elapsed += interval

        logger.warning("wait_for_text がタイムアウト: %s", text)

    async def _wait_for_still(self, timeout: float) -> None:
        """画面変化が収まるまで待機する。

        連続するスクリーンショットの画像ハッシュを比較し、
        指定回数連続で変化がなければ安定とみなす。
        """
        check_interval = 0.3
        min_stable_frames = 3
        stable_count = 0
        prev_hash = ""
        elapsed = 0.0

        while elapsed < timeout:
            ss = self._backend.capture_screen()
            current_hash = self._image_hash(ss.image_bytes)

            if prev_hash and current_hash == prev_hash:
                stable_count += 1
                if stable_count >= min_stable_frames:
                    logger.debug("画面が安定 (wait_for_still: %.1fs)", elapsed)
                    return
            else:
                stable_count = 0

            prev_hash = current_hash
            await asyncio.sleep(check_interval)
            elapsed += check_interval

        logger.debug("wait_for_still がタイムアウト (%.1fs経過)", timeout)

    # ── 画像ユーティリティ ─────────────────────────────

    @staticmethod
    def _image_hash(image_bytes: bytes) -> str:
        """画像のSHA256ハッシュを返す（変化検出用）。"""
        return hashlib.sha256(image_bytes).hexdigest()
