"""アクション実行者 — ActionをDisplayBackendの操作に変換して実行する。

エージェントループから Action を受け取り、
DisplayBackend（VNCクライアント等）を通じてVM上で実行する。
"""

import asyncio
import time
import logging
from typing import Any

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.vm.base import DisplayBackend

logger = logging.getLogger(__name__)


class ActionExecutor:
    """ActionをVM上で実行するエンジン。

    DisplayBackend に依存し、各ActionTypeを適切な操作に変換する。
    """

    def __init__(self, backend: DisplayBackend) -> None:
        self._backend = backend

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
                    p["start_x"], p["start_y"],
                    p["end_x"], p["end_y"],
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
        """画面に指定テキストが現れるまで待機（現状は単純スリープで代用）。

        将来的にはOCRと組み合わせて実装する。
        """
        elapsed = 0.0
        interval = 0.5
        while elapsed < timeout:
            await asyncio.sleep(interval)
            elapsed += interval
            # TODO: OCRで画面からテキストを抽出し、textの出現を確認
        logger.warning("wait_for_text がタイムアウト: %s", text)

    async def _wait_for_still(self, timeout: float) -> None:
        """画面変化が収まるまで待機（現状は単純スリープで代用）。

        将来的には画像差分検出と組み合わせて実装する。
        """
        await asyncio.sleep(min(timeout, 3.0))
        # TODO: 連続するスクリーンショットの差分が閾値以下になるまで待つ
