"""Action primitives のテスト。"""

import pytest

from ai_desktop_agent.actions.primitives import (
    _REQUIRED_PARAMS,
    ALL_ACTION_TYPES,
    Action,
    ActionType,
)


class TestActionType:
    """ActionType enum のテスト。"""

    def test_all_types_have_required_params_defined(self):
        """すべての ActionType が _REQUIRED_PARAMS に定義されていること。"""
        for at in ALL_ACTION_TYPES:
            assert at in _REQUIRED_PARAMS, f"{at} が _REQUIRED_PARAMS に未定義"

    def test_all_types_are_unique(self):
        """値の重複がないこと。"""
        values = [at.value for at in ActionType]
        assert len(values) == len(set(values))

    def test_subtask_complete_exists(self):
        """メタアクション SUBTASK_COMPLETE が存在すること。"""
        assert ActionType.SUBTASK_COMPLETE in ALL_ACTION_TYPES


class TestActionValidation:
    """Action のバリデーションテスト。"""

    def test_valid_click_no_position(self):
        """クリック系アクションは座標なしでも有効。"""
        action = Action(action_type=ActionType.LEFT_CLICK)
        assert action.action_type == ActionType.LEFT_CLICK
        assert action.params == {}

    def test_valid_click_with_position(self):
        """クリック系アクションは座標付きでも有効。"""
        action = Action(action_type=ActionType.LEFT_CLICK, params={"x": 100, "y": 200})
        assert action.params["x"] == 100

    def test_missing_required_params_raises(self):
        """必須パラメータが不足すると ValueError。"""
        with pytest.raises(ValueError, match="必須パラメータが不足"):
            Action(action_type=ActionType.MOUSE_MOVE, params={})

    def test_unknown_params_raises(self):
        """不明なパラメータがあると ValueError。"""
        with pytest.raises(ValueError, match="不明なパラメータ"):
            Action(
                action_type=ActionType.LEFT_CLICK,
                params={"x": 100, "y": 200, "bogus": "xxx"},
            )

    def test_type_missing_text(self):
        """TYPE は text が必須。"""
        with pytest.raises(ValueError, match="必須パラメータが不足"):
            Action(action_type=ActionType.TYPE, params={})

    def test_key_press_missing_key(self):
        """KEY_PRESS は key が必須。"""
        with pytest.raises(ValueError, match="必須パラメータが不足"):
            Action(action_type=ActionType.KEY_PRESS, params={})

    def test_key_combo_missing_keys(self):
        """KEY_COMBO は keys が必須。"""
        with pytest.raises(ValueError, match="必須パラメータが不足"):
            Action(action_type=ActionType.KEY_COMBO, params={})

    def test_wait_missing_seconds(self):
        """WAIT は seconds が必須。"""
        with pytest.raises(ValueError, match="必須パラメータが不足"):
            Action(action_type=ActionType.WAIT, params={})

    def test_drag_missing_params(self):
        """DRAG は4つの座標すべてが必須。"""
        with pytest.raises(ValueError):
            Action(
                action_type=ActionType.DRAG,
                params={"start_x": 0, "start_y": 0},  # end_x, end_y 欠落
            )

    def test_scroll_missing_direction(self):
        """SCROLL は direction と amount が必須。"""
        with pytest.raises(ValueError):
            Action(action_type=ActionType.SCROLL, params={"amount": 100})


class TestActionDescription:
    """自動説明生成のテスト。"""

    def test_generates_description_automatically(self):
        """空の description は自動生成される。"""
        action = Action(action_type=ActionType.LEFT_CLICK, params={"x": 50, "y": 60})
        assert "50" in action.description
        assert "左クリック" in action.description

    def test_preserves_explicit_description(self):
        """明示的に与えた description はそのまま保持される。"""
        action = Action(
            action_type=ActionType.LEFT_CLICK,
            description="ファイルメニューをクリック",
        )
        assert action.description == "ファイルメニューをクリック"

    def test_type_description_truncates_long_text(self):
        """長いテキストは説明で切り詰められる。"""
        long_text = "a" * 100
        action = Action(action_type=ActionType.TYPE, params={"text": long_text})
        assert len(action.description) <= 50  # 30 + "..." + 前後の文字

    def test_subtask_complete_description(self):
        """SUBTASK_COMPLETE の説明。"""
        action = Action(action_type=ActionType.SUBTASK_COMPLETE)
        assert "サブタスク完了" in action.description


class TestActionImmutability:
    """Action は frozen dataclass なのでフィールド再代入不可。"""

    def test_cannot_reassign_action_type(self):
        """action_type 再代入で FrozenInstanceError。"""
        action = Action(action_type=ActionType.LEFT_CLICK)
        with pytest.raises(Exception):  # noqa: B017
            action.action_type = ActionType.RIGHT_CLICK  # type: ignore

    def test_cannot_reassign_params(self):
        """params フィールドの再代入で FrozenInstanceError。"""
        action = Action(action_type=ActionType.LEFT_CLICK)
        with pytest.raises(Exception):  # noqa: B017
            action.params = {"x": 100}  # type: ignore


class TestAllActionTypesValid:
    """全 ActionType に対して有効な Action が作れることの網羅テスト。"""

    VALID_PARAMS_MAP = {
        ActionType.MOUSE_MOVE: {"x": 0, "y": 0},
        ActionType.LEFT_CLICK: {},
        ActionType.RIGHT_CLICK: {},
        ActionType.DOUBLE_CLICK: {},
        ActionType.MIDDLE_CLICK: {},
        ActionType.DRAG: {"start_x": 0, "start_y": 0, "end_x": 100, "end_y": 100},
        ActionType.SCROLL: {"direction": "down", "amount": 100},
        ActionType.TYPE: {"text": "hello"},
        ActionType.KEY_PRESS: {"key": "enter"},
        ActionType.KEY_COMBO: {"keys": ["ctrl", "c"]},
        ActionType.KEY_HOLD: {"key": "shift", "duration_ms": 500},
        ActionType.WAIT: {"seconds": 1.0},
        ActionType.WAIT_FOR_TEXT: {"text": "Loading...", "timeout": 10.0},
        ActionType.WAIT_FOR_STILL: {"timeout": 5.0},
        ActionType.SCREENSHOT: {},
        ActionType.SUBTASK_COMPLETE: {},
    }

    @pytest.mark.parametrize("action_type", list(ALL_ACTION_TYPES))
    def test_valid_action(self, action_type):
        """各アクション種別について有効な Action が作成できる。"""
        params = self.VALID_PARAMS_MAP[action_type]
        action = Action(action_type=action_type, params=params)
        assert action.action_type == action_type
        assert action.description  # 空でない説明が自動生成される
