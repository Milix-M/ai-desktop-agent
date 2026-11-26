"""Action primitives のテスト。"""

import pytest

from ai_desktop_agent.actions.primitives import (
    ALL_ACTION_TYPES,
    Action,
    ActionType,
    required_params,
)


class TestActionType:
    """ActionType enum のテスト。"""

    def test_editing_a_callers_schema_cannot_disable_required_click_coordinates(self):
        schema = required_params(ActionType.LEFT_CLICK)
        assert schema == {"x", "y"}
        schema.clear()
        with pytest.raises(ValueError, match="必須パラメータ"):
            Action(ActionType.LEFT_CLICK)
        schema.add("unrecognized")
        assert required_params(ActionType.LEFT_CLICK) == {"x", "y"}
        valid = Action(ActionType.LEFT_CLICK, {"x": 30, "y": 40})
        assert valid.params == {"x": 30, "y": 40}

    def test_all_types_are_unique(self):
        """値の重複がないこと。"""
        values = [at.value for at in ActionType]
        assert len(values) == len(set(values))

    def test_subtask_complete_exists(self):
        """メタアクション SUBTASK_COMPLETE が存在すること。"""
        assert ActionType.SUBTASK_COMPLETE in ALL_ACTION_TYPES


class TestActionValidation:
    """Action のバリデーションテスト。"""

    def test_click_requires_position(self):
        """クリック系アクションは座標必須（空打ち防止）。"""
        with pytest.raises(ValueError, match="必須パラメータが不足"):
            Action(action_type=ActionType.LEFT_CLICK)
        with pytest.raises(ValueError, match="必須パラメータが不足"):
            Action(action_type=ActionType.RIGHT_CLICK, params={"x": 100})

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
            params={"x": 10, "y": 20},
            description="ファイルメニューをクリック",
        )
        assert action.description == "ファイルメニューをクリック"

    def test_type_description_does_not_preview_input(self):
        """入力内容を説明へ複製しない。"""
        long_text = "a" * 100
        action = Action(action_type=ActionType.TYPE, params={"text": long_text})
        assert "aaaa" not in action.description
        assert "100文字" in action.description

    def test_subtask_complete_description(self):
        """SUBTASK_COMPLETE の説明。"""
        action = Action(action_type=ActionType.SUBTASK_COMPLETE)
        assert "サブタスク完了" in action.description


class TestActionImmutability:
    """Action は frozen dataclass なのでフィールド再代入不可。"""

    def test_cannot_reassign_action_type(self):
        """action_type 再代入で FrozenInstanceError。"""
        action = Action(action_type=ActionType.LEFT_CLICK, params={"x": 0, "y": 0})
        with pytest.raises(Exception):  # noqa: B017
            action.action_type = ActionType.RIGHT_CLICK  # type: ignore

    def test_cannot_reassign_params(self):
        """params フィールドの再代入で FrozenInstanceError。"""
        action = Action(action_type=ActionType.LEFT_CLICK, params={"x": 0, "y": 0})
        with pytest.raises(Exception):  # noqa: B017
            action.params = {"x": 100}  # type: ignore


class TestAllActionTypesValid:
    """全 ActionType に対して有効な Action が作れることの網羅テスト。"""

    VALID_PARAMS_MAP = {
        ActionType.MOUSE_MOVE: {"x": 0, "y": 0},
        ActionType.LEFT_CLICK: {"x": 0, "y": 0},
        ActionType.ELEMENT_CLICK: {"ref": "1.1"},
        ActionType.FOCUS_WINDOW: {"ref": "1.2"},
        ActionType.TABLE_TYPE: {"ref": "1.2", "sql": "select 1", "separator": " | "},
        ActionType.WORKSPACE_READ: {"ref": "1.2", "items": [{"source": "a"}]},
        ActionType.WORKSPACE_MOVE: {"ref": "1.2", "items": [{"source": "a", "target": "b"}]},
        ActionType.SOURCE_READ: {"ref": "1.2", "source": 1, "search": "", "offset": 0},
        ActionType.NOTE_SAVE: {"title": "source", "text": "checked"},
        ActionType.NOTE_READ: {"title": "source"},
        ActionType.TABLE_QUERY: {"ref": "1.2", "sql": "select 1"},
        ActionType.TABLE_FILL: {
            "ref": "1.2",
            "sql": "select 1",
            "start": "A1",
            "formulas": [],
            "save": True,
        },
        ActionType.CALC_WRITE: {"ref": "1.2", "cells": [{"address": "B2", "value": "=1+1"}]},
        ActionType.RIGHT_CLICK: {"x": 0, "y": 0},
        ActionType.DOUBLE_CLICK: {"x": 0, "y": 0},
        ActionType.MIDDLE_CLICK: {"x": 0, "y": 0},
        ActionType.DRAG: {"start_x": 0, "start_y": 0, "end_x": 100, "end_y": 100},
        ActionType.SCROLL: {"direction": "down", "amount": 100},
        ActionType.TYPE: {"text": "hello"},
        ActionType.REQUEST_USER_INPUT: {"reason": "ログインしてください"},
        ActionType.KEY_PRESS: {"key": "enter"},
        ActionType.KEY_COMBO: {"keys": ["ctrl", "c"]},
        ActionType.KEY_HOLD: {"key": "shift", "duration_ms": 500},
        ActionType.WAIT: {"seconds": 1.0},
        ActionType.WAIT_FOR_STILL: {"timeout": 5.0},
        ActionType.SCREENSHOT: {},
        ActionType.REGION_SELECT: {"x": 0, "y": 0, "width": 200, "height": 200},
        ActionType.VM_RESTART: {"reason": "stuck"},
        ActionType.SUBTASK_COMPLETE: {},
    }

    @pytest.mark.parametrize("action_type", list(ALL_ACTION_TYPES))
    def test_valid_action(self, action_type):
        """各アクション種別について有効な Action が作成できる。"""
        params = self.VALID_PARAMS_MAP[action_type]
        action = Action(action_type=action_type, params=params)
        assert action.action_type == action_type
        assert action.description  # 空でない説明が自動生成される


@pytest.mark.parametrize(
    "cells",
    [
        [],
        [{"address": "XFE1", "value": "x"}],
        [{"address": "A1048577", "value": "x"}],
        [{"address": "a1", "value": "x"}],
        [{"address": "A1", "value": "x\ty"}],
        [{"address": "A1", "value": "x\ny"}],
        [{"address": "A1", "value": "x" * 257}],
        [{"address": "A1", "value": "x"}, {"address": "A1", "value": "y"}],
        [{"address": "A1", "value": 2}],
        [{"address": "A1", "value": "x", "extra": 1}],
        [{"address": f"B{i}", "value": "a"} for i in range(1, 34)],
    ],
)
def test_calc_batch_validates_all_cells_before_input(cells):
    with pytest.raises(ValueError):
        Action(ActionType.CALC_WRITE, {"ref": "3.1", "cells": cells})
