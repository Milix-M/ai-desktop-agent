"""VNC操作精度の回帰テスト。

ズーム座標逆変換・クランプ・連続クリック防止・空振り検出が
正しく動くことを保証する。
"""

import io

import pytest
from PIL import Image

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.types import ActionDecision
from ai_desktop_agent.agent.state import Goal, Subtask
from ai_desktop_agent.vm.overlay import crop_region_with_meta
from ai_desktop_agent.vm.screenshot import Screenshot


def _real_ss(w: int = 1024, h: int = 768) -> Screenshot:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return Screenshot(image_bytes=buf.getvalue(), width=w, height=h)


class TestZoomMapping:
    def test_actual_scale_returned(self):
        ss = _real_ss()
        zoomed, scale = crop_region_with_meta(ss, 100, 100, 200, 200, scale=2.0)
        assert scale == 2.0
        assert (zoomed.width, zoomed.height) == (400, 400)

    def test_max_dim_clamp_keeps_mapping(self):
        ss = _real_ss()
        zoomed, scale = crop_region_with_meta(ss, 0, 0, 600, 600, scale=2.0, max_dim=1024)
        assert abs(scale - 1024 / 600) < 0.01
        # 拡大画像中央 -> 絶対300,300に戻る
        assert abs(int((zoomed.width // 2) / scale) - 300) <= 1

    def test_click_requires_coords(self):
        with pytest.raises(ValueError, match="必須パラメータ"):
            Action(action_type=ActionType.LEFT_CLICK)
        with pytest.raises(ValueError, match="必須パラメータ"):
            Action(action_type=ActionType.RIGHT_CLICK, params={"x": 1})


class TestSessionGuards:
    @staticmethod
    def _session():
        from ai_desktop_agent.agent.loop import AgentLoop
        from ai_desktop_agent.server.session import TaskSession
        from ai_desktop_agent.vm.fake import FakeDisplayBackend

        s = TaskSession.__new__(TaskSession)
        s.loop = AgentLoop()
        s.display = FakeDisplayBackend()
        s._last_before_raw = None
        s.loop.start(Goal(description="t"))
        s.loop.understanding_done()
        s.loop.plan_ready([Subtask(id="s1", description="d")])
        return s

    def test_clamp_to_screen(self):
        s = self._session()
        d = ActionDecision(
            action=Action(action_type=ActionType.LEFT_CLICK, params={"x": 5000, "y": -5}),
            expected_effect="",
            confidence=1.0,
            reasoning="",
        )
        out = s._clamp_decision(d, _real_ss())
        assert out.action.params == {"x": 1023, "y": 0}

    def test_repeat_click_detected(self):
        s = self._session()
        s.loop.record_action(
            Action(action_type=ActionType.LEFT_CLICK, params={"x": 100, "y": 100}),
            True,
        )
        assert (
            s._is_repeat_click(
                Action(action_type=ActionType.LEFT_CLICK, params={"x": 103, "y": 102})
            )
            is True
        )
        assert (
            s._is_repeat_click(
                Action(action_type=ActionType.LEFT_CLICK, params={"x": 500, "y": 500})
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_verify_fails_on_no_change(self):
        from ai_desktop_agent.agent.state import ActionRecord

        s = self._session()
        raw = s.display.capture_raw()
        s._last_before_raw = raw
        # Fakeは同一画像を返すため変化なし -> False
        rec = ActionRecord(
            action=Action(action_type=ActionType.LEFT_CLICK, params={"x": 1, "y": 1}),
            success=True,
        )
        assert await s._verify_click_effect(rec) is False
