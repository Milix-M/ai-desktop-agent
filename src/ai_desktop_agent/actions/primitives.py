"""アクション型定義 — エージェントがVMに対して実行する基本操作。

すべてのアクションはこのモジュールで定義された型を使う。
LLMの出力もこの型にパースされる。
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ActionType(StrEnum):
    """エージェントが実行可能なアクションの種類。"""

    # === マウス操作 ===
    MOUSE_MOVE = "mouse_move"
    LEFT_CLICK = "left_click"
    RIGHT_CLICK = "right_click"
    DOUBLE_CLICK = "double_click"
    MIDDLE_CLICK = "middle_click"
    DRAG = "drag"
    SCROLL = "scroll"

    # === キーボード操作 ===
    TYPE = "type"
    KEY_PRESS = "key_press"
    KEY_COMBO = "key_combo"
    KEY_HOLD = "key_hold"

    # === 待機 ===
    WAIT = "wait"
    WAIT_FOR_TEXT = "wait_for_text"
    WAIT_FOR_STILL = "wait_for_still"

    # === 観測 ===
    SCREENSHOT = "screenshot"

    # === メタ ===
    SUBTASK_COMPLETE = "subtask_complete"


# アクション種別ごとの必須パラメータ定義
_REQUIRED_PARAMS: dict[ActionType, set[str]] = {
    ActionType.MOUSE_MOVE: {"x", "y"},
    ActionType.LEFT_CLICK: set(),  # x, y はオプション（省略時は現在位置）
    ActionType.RIGHT_CLICK: set(),
    ActionType.DOUBLE_CLICK: set(),
    ActionType.MIDDLE_CLICK: set(),
    ActionType.DRAG: {"start_x", "start_y", "end_x", "end_y"},
    ActionType.SCROLL: {"direction", "amount"},
    ActionType.TYPE: {"text"},
    ActionType.KEY_PRESS: {"key"},
    ActionType.KEY_COMBO: {"keys"},
    ActionType.KEY_HOLD: {"key", "duration_ms"},
    ActionType.WAIT: {"seconds"},
    ActionType.WAIT_FOR_TEXT: {"text", "timeout"},
    ActionType.WAIT_FOR_STILL: {"timeout"},
    ActionType.SCREENSHOT: set(),
    ActionType.SUBTASK_COMPLETE: set(),
}

# オプショナルなパラメータ（バリデーションでエラーにしない）
_OPTIONAL_PARAMS: dict[ActionType, set[str]] = {
    ActionType.LEFT_CLICK: {"x", "y"},
    ActionType.RIGHT_CLICK: {"x", "y"},
    ActionType.DOUBLE_CLICK: {"x", "y"},
    ActionType.MIDDLE_CLICK: {"x", "y"},
}


@dataclass(frozen=True)
class Action:
    """エージェントがVMに対して実行する1つの操作。

    Attributes:
        action_type: アクションの種類。
        params: アクション種別に応じたパラメータ。
        description: 人間向けの説明（ログ表示用）。省略時は自動生成。
    """

    action_type: ActionType
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""

    def __post_init__(self) -> None:
        """バリデーション: 必須パラメータのチェック。"""
        if self.action_type not in _REQUIRED_PARAMS:
            raise ValueError(f"不明なアクション種別: {self.action_type}")

        required = _REQUIRED_PARAMS[self.action_type]
        optional = _OPTIONAL_PARAMS.get(self.action_type, set())
        valid_keys = required | optional
        provided = set(self.params.keys())

        missing = required - provided
        if missing:
            raise ValueError(f"{self.action_type.value} に必須パラメータが不足: {missing}")

        unknown = provided - valid_keys
        if unknown:
            raise ValueError(f"{self.action_type.value} に不明なパラメータ: {unknown}")

        # description が空なら自動生成
        if not self.description:
            object.__setattr__(self, "description", self._generate_description())

    def _generate_description(self) -> str:
        """アクションの人間向け説明を自動生成。"""
        match self.action_type:
            case ActionType.MOUSE_MOVE:
                return f"カーソルを ({self.params['x']}, {self.params['y']}) に移動"
            case ActionType.LEFT_CLICK:
                if "x" in self.params:
                    return f"座標 ({self.params['x']}, {self.params['y']}) を左クリック"
                return "現在位置を左クリック"
            case ActionType.RIGHT_CLICK:
                if "x" in self.params:
                    return f"座標 ({self.params['x']}, {self.params['y']}) を右クリック"
                return "現在位置を右クリック"
            case ActionType.DOUBLE_CLICK:
                if "x" in self.params:
                    return f"座標 ({self.params['x']}, {self.params['y']}) をダブルクリック"
                return "現在位置をダブルクリック"
            case ActionType.MIDDLE_CLICK:
                if "x" in self.params:
                    return f"座標 ({self.params['x']}, {self.params['y']}) を中クリック"
                return "現在位置を中クリック"
            case ActionType.DRAG:
                return (
                    f"ドラッグ: ({self.params['start_x']}, {self.params['start_y']}) → "
                    f"({self.params['end_x']}, {self.params['end_y']})"
                )
            case ActionType.SCROLL:
                return f"{self.params['direction']}方向に {self.params['amount']}px スクロール"
            case ActionType.TYPE:
                text = self.params["text"]
                display = text[:30] + "..." if len(text) > 30 else text
                return f"「{display}」と入力"
            case ActionType.KEY_PRESS:
                return f"キー押下: {self.params['key']}"
            case ActionType.KEY_COMBO:
                return f"キーコンボ: {'+'.join(self.params['keys'])}"
            case ActionType.KEY_HOLD:
                return f"キー長押し: {self.params['key']} ({self.params['duration_ms']}ms)"
            case ActionType.WAIT:
                return f"{self.params['seconds']}秒待機"
            case ActionType.WAIT_FOR_TEXT:
                timeout_s = self.params["timeout"]
                return f"「{self.params['text']}」が表示されるまで待機 (timeout={timeout_s}s)"
            case ActionType.WAIT_FOR_STILL:
                return f"画面変化が収まるまで待機 (timeout={self.params['timeout']}s)"
            case ActionType.SCREENSHOT:
                return "スクリーンショット取得"
            case ActionType.SUBTASK_COMPLETE:
                return "サブタスク完了"


ALL_ACTION_TYPES: frozenset[ActionType] = frozenset(ActionType)
