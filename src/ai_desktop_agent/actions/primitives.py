"""アクションの基本データ型。

エージェントループと LLM がやりとりするアクション定義。
"""

from __future__ import annotations

import dataclasses
from enum import Enum


class ActionType(Enum):
    """エージェントが実行可能な操作の種類。"""

    # マウス操作（直接座標指定）
    MOUSE_MOVE = "mouse_move"
    LEFT_CLICK = "left_click"
    RIGHT_CLICK = "right_click"
    MIDDLE_CLICK = "middle_click"
    DOUBLE_CLICK = "double_click"
    DRAG = "drag"
    SCROLL = "scroll"

    # キーボード操作
    TYPE = "type"
    KEY_PRESS = "key_press"
    KEY_COMBO = "key_combo"
    KEY_HOLD = "key_hold"

    # 制御
    WAIT = "wait"
    WAIT_FOR_TEXT = "wait_for_text"
    WAIT_FOR_STILL = "wait_for_still"
    SCREENSHOT = "screenshot"
    SUBTASK_COMPLETE = "subtask_complete"

    # ズームワークフロー（新設）
    REGION_SELECT = "region_select"  # 精密クリックの前に領域を拡大表示


# 全アクション種別リスト（テスト用）
ALL_ACTION_TYPES = list(ActionType)

# アクション種別ごとの必須パラメータ
_REQUIRED_PARAMS: dict[ActionType, set[str]] = {
    ActionType.MOUSE_MOVE: {"x", "y"},
    ActionType.LEFT_CLICK: set(),
    ActionType.RIGHT_CLICK: set(),
    ActionType.MIDDLE_CLICK: set(),
    ActionType.DOUBLE_CLICK: set(),
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
    ActionType.REGION_SELECT: {"x", "y", "width", "height"},
}

# アクション種別ごとの任意パラメータ
_OPTIONAL_PARAMS: dict[ActionType, set[str]] = {
    ActionType.LEFT_CLICK: {"x", "y"},
    ActionType.RIGHT_CLICK: {"x", "y"},
    ActionType.MIDDLE_CLICK: {"x", "y"},
    ActionType.DOUBLE_CLICK: {"x", "y"},
    ActionType.MOUSE_MOVE: set(),
    ActionType.DRAG: set(),
    ActionType.SCROLL: set(),
    ActionType.TYPE: set(),
    ActionType.KEY_PRESS: set(),
    ActionType.KEY_COMBO: set(),
    ActionType.KEY_HOLD: set(),
    ActionType.WAIT: set(),
    ActionType.WAIT_FOR_TEXT: set(),
    ActionType.WAIT_FOR_STILL: set(),
    ActionType.SCREENSHOT: set(),
    ActionType.SUBTASK_COMPLETE: set(),
    ActionType.REGION_SELECT: set(),
}

# 日本語のアクション名（説明文自動生成用）
_ACTION_NAMES: dict[ActionType, str] = {
    ActionType.MOUSE_MOVE: "マウス移動",
    ActionType.LEFT_CLICK: "左クリック",
    ActionType.RIGHT_CLICK: "右クリック",
    ActionType.MIDDLE_CLICK: "中クリック",
    ActionType.DOUBLE_CLICK: "ダブルクリック",
    ActionType.DRAG: "ドラッグ",
    ActionType.SCROLL: "スクロール",
    ActionType.TYPE: "テキスト入力",
    ActionType.KEY_PRESS: "キー押下",
    ActionType.KEY_COMBO: "キーコンボ",
    ActionType.KEY_HOLD: "キー長押し",
    ActionType.WAIT: "待機",
    ActionType.WAIT_FOR_TEXT: "テキスト待機",
    ActionType.WAIT_FOR_STILL: "画面安定待機",
    ActionType.SCREENSHOT: "スクリーンショット",
    ActionType.SUBTASK_COMPLETE: "サブタスク完了",
    ActionType.REGION_SELECT: "領域拡大要求",
}


@dataclasses.dataclass(frozen=True)
class Action:
    """単一の操作指示。"""

    action_type: ActionType
    params: dict | None = None
    description: str = ""

    def __post_init__(self) -> None:
        params = self.params or {}

        # 必須パラメータ検証
        required = _REQUIRED_PARAMS.get(self.action_type, set())
        missing = required - set(params.keys())
        if missing:
            raise ValueError(
                f"必須パラメータが不足しています: {missing} (action_type={self.action_type.value})"
            )

        # 不明パラメータ検証
        optional = _OPTIONAL_PARAMS.get(self.action_type, set())
        allowed = required | optional
        unknown = set(params.keys()) - allowed
        if unknown:
            raise ValueError(
                f"不明なパラメータです: {unknown} (action_type={self.action_type.value})"
            )

        # 説明文の自動生成
        if not self.description:
            object.__setattr__(self, "description", _generate_description(self.action_type, params))


def _generate_description(action_type: ActionType, params: dict) -> str:
    """アクション種別とパラメータから人が読める説明文を生成する。"""
    name = _ACTION_NAMES.get(action_type, action_type.value)

    if action_type == ActionType.MOUSE_MOVE:
        return f"{name} ({params.get('x')}, {params.get('y')})"
    elif action_type in (
        ActionType.LEFT_CLICK,
        ActionType.RIGHT_CLICK,
        ActionType.MIDDLE_CLICK,
        ActionType.DOUBLE_CLICK,
    ):
        if "x" in params and "y" in params:
            return f"{name} ({params['x']}, {params['y']})"
        return f"{name}（現在位置）"
    elif action_type == ActionType.TYPE:
        text = str(params.get("text", ""))
        if len(text) > 30:
            text = text[:27] + "..."
        return f'{name}: "{text}"'
    elif action_type == ActionType.KEY_PRESS:
        return f"{name}: {params.get('key', '')}"
    elif action_type == ActionType.KEY_COMBO:
        keys = params.get("keys", [])
        return f"{name}: {'+'.join(keys)}"
    elif action_type == ActionType.DRAG:
        return (
            f"{name} "
            f"({params.get('start_x')},{params.get('start_y')})"
            f"→({params.get('end_x')},{params.get('end_y')})"
        )
    elif action_type == ActionType.REGION_SELECT:
        return (
            f"{name} "
            f"領域({params.get('x')},{params.get('y')}) "
            f"{params.get('width')}x{params.get('height')}"
        )
    else:
        return name
