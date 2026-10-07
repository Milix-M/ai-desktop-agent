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
    # 注意: SCREENSHOT は毎ターン自動撮影されるため LLM には提示しない
    # （プロンプト・スキーマから除外済み）。enumと実行器は互換のため残す。
    SCREENSHOT = "screenshot"
    SUBTASK_COMPLETE = "subtask_complete"

    # ズームワークフロー（新設）
    REGION_SELECT = "region_select"  # 精密クリックの前に領域を拡大表示

    # VM操作（ユーザー許可制）
    VM_RESTART = "vm_restart"  # VM作り直し（allow_vm_restart 時のみ実行）


# 全アクション種別リスト（テスト用）
ALL_ACTION_TYPES = list(ActionType)


def allowed_params(action_type: ActionType) -> set[str]:
    """指定アクション種別で有効なパラメータ名の集合を返す。"""
    return set(_REQUIRED_PARAMS.get(action_type, set())) | set(
        _OPTIONAL_PARAMS.get(action_type, set())
    )


def required_params(action_type: ActionType) -> set[str]:
    """指定アクション種別の必須パラメータ名の集合を返す。"""
    return set(_REQUIRED_PARAMS.get(action_type, set()))


# クリック系アクション（座標必須）
CLICK_ACTIONS: frozenset[ActionType] = frozenset(
    {
        ActionType.LEFT_CLICK,
        ActionType.RIGHT_CLICK,
        ActionType.MIDDLE_CLICK,
        ActionType.DOUBLE_CLICK,
    }
)

# 座標キー（絶対座標系）
_X_KEYS: frozenset[str] = frozenset({"x", "start_x", "end_x"})
_Y_KEYS: frozenset[str] = frozenset({"y", "start_y", "end_y"})

# アクション種別ごとの必須パラメータ
# クリック系は座標必須。空パラメータでの「現在位置クリック」は
# 誤クリックの温床になるため禁止する（#26 のスキーマと整合）。
_REQUIRED_PARAMS: dict[ActionType, set[str]] = {
    ActionType.MOUSE_MOVE: {"x", "y"},
    ActionType.LEFT_CLICK: {"x", "y"},
    ActionType.RIGHT_CLICK: {"x", "y"},
    ActionType.MIDDLE_CLICK: {"x", "y"},
    ActionType.DOUBLE_CLICK: {"x", "y"},
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
    ActionType.VM_RESTART: {"reason"},
}

# アクション種別ごとの任意パラメータ
_OPTIONAL_PARAMS: dict[ActionType, set[str]] = {
    ActionType.LEFT_CLICK: set(),
    ActionType.RIGHT_CLICK: set(),
    ActionType.MIDDLE_CLICK: set(),
    ActionType.DOUBLE_CLICK: set(),
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
    ActionType.VM_RESTART: "VM作り直し",
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
        # 座標は必須（バリデーション済み）。欠落時はフォールバック表示。
        if "x" in params and "y" in params:
            return f"{name} ({params['x']}, {params['y']})"
        return f"{name}（座標不明）"
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
    elif action_type == ActionType.VM_RESTART:
        return f"{name}: {params.get('reason', '')}"
    else:
        return name
