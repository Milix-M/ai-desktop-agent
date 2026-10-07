"""手抜き完了ガードとステップ上限のテスト。"""

import asyncio

import pytest

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision
from ai_desktop_agent.agent.state import AgentState
from ai_desktop_agent.server.session import MAX_ACTIONS_PER_TASK, TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend


def _decision(action_type: ActionType, params: dict | None = None) -> ActionDecision:
    return ActionDecision(
        action=Action(action_type=action_type, params=params or {}),
        expected_effect="test",
        confidence=0.9,
        reasoning="test",
    )


class _SequenceLLM(MockLLMProvider):
    """decide_next_action をキュー順に返すモック。"""

    def __init__(self, decisions: list[ActionDecision]):
        super().__init__()
        self._queue = list(decisions)

    async def decide_next_action(self, *args, **kwargs):
        self._decide_calls.append(args)
        if self._queue:
            return self._queue.pop(0)
        return _decision(ActionType.SUBTASK_COMPLETE)


def _click_decision(x=100, y=200) -> ActionDecision:
    return _decision(ActionType.LEFT_CLICK, {"x": x, "y": y})


class TestEmptyCompleteGuard:
    @pytest.mark.asyncio
    async def test_empty_complete_is_refused_once(self, tmp_path):
        """操作なし完了は却下され、実操作後に受け入れられる。"""
        llm = _SequenceLLM(
            [
                _decision(ActionType.SUBTASK_COMPLETE),  # 却下される
                _click_decision(),
                _decision(ActionType.SUBTASK_COMPLETE),  # 受理される
            ]
        )
        store = TaskStore(root=tmp_path / "data")
        session = TaskSession(llm=llm, display=FakeDisplayBackend(), store=store)
        await session.run("ガードテスト")
        rec = store.load(session.id)
        assert rec is not None
        types = [a.action_type for a in rec.actions]
        assert "left_click" in types
        assert llm.decide_call_count == 3

    @pytest.mark.asyncio
    async def test_stubborn_complete_eventually_accepted(self, tmp_path):
        """3回連続の完了宣言は受け入れる（デッドロック防止）。"""
        llm = _SequenceLLM([_decision(ActionType.SUBTASK_COMPLETE)] * 5)
        store = TaskStore(root=tmp_path / "data")
        session = TaskSession(llm=llm, display=FakeDisplayBackend(), store=store)
        result = await session.run("頑固テスト")
        assert result is True
        assert llm.decide_call_count == 3

    def test_is_empty_complete(self):
        session = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend())
        session.loop.start(
            __import__("ai_desktop_agent.agent.state", fromlist=["Goal"]).Goal(description="g")
        )
        assert session._is_empty_complete(_decision(ActionType.SUBTASK_COMPLETE)) is True
        assert session._is_empty_complete(_click_decision()) is False
        session.loop.record_action(
            Action(action_type=ActionType.LEFT_CLICK, params={"x": 1, "y": 2}), True
        )
        assert session._is_empty_complete(_decision(ActionType.SUBTASK_COMPLETE)) is False


class TestMaxActionsCap:
    @pytest.mark.asyncio
    async def test_cap_triggers_recovery(self):
        session = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend())
        from ai_desktop_agent.agent.state import Goal, Subtask

        session.loop.start(Goal(description="g"))
        session.loop.understanding_done()
        session.loop.plan_ready([Subtask(id="s1", description="d")])
        for _ in range(MAX_ACTIONS_PER_TASK):
            session.loop.record_action(
                Action(action_type=ActionType.WAIT, params={"seconds": 0.01}), True
            )
        await session._execute_phase()
        assert session.loop.state == AgentState.FAILED


class TestReasoningPersistence:
    def test_record_carries_metadata(self, tmp_path):
        store = TaskStore(root=tmp_path / "data")
        session = TaskSession(llm=MockLLMProvider(), display=FakeDisplayBackend(), store=store)
        session.loop.record_action(
            Action(action_type=ActionType.LEFT_CLICK, params={"x": 1, "y": 2}),
            True,
            reasoning="ボタンを押す",
            confidence=0.8,
        )
        rec = session.snapshot()
        assert rec.actions[0].reasoning == "ボタンを押す"
        assert rec.actions[0].confidence == 0.8
        store.save(rec)
        assert store.load(rec.id).actions[0].reasoning == "ボタンを押す"


class TestZoomDepthCap:
    @pytest.mark.asyncio
    async def test_nested_region_select_gives_up(self):
        """入れ子の region_select が続いても深追いせず None を返す。"""
        from ai_desktop_agent.agent.state import Subtask

        def _region() -> ActionDecision:
            return _decision(
                ActionType.REGION_SELECT, {"x": 10, "y": 10, "width": 200, "height": 200}
            )

        llm = _SequenceLLM([_region(), _region(), _region()])
        session = TaskSession(llm=llm, display=FakeDisplayBackend())
        out = await session._handle_region_zoom(Subtask(id="s1", description="d"), _region())
        assert out is None
        # Lv0, Lv1 の2回だけ問い合わせる
        assert llm.decide_call_count == 2


class TestJpegConversion:
    def test_to_jpeg_keeps_dimensions(self):
        import io
        import random

        from PIL import Image

        from ai_desktop_agent.agent.llm.openai_compat_provider import (
            OpenAICompatProvider,
        )

        buf = io.BytesIO()
        # ノイズ画像で実画面に近づける（ベタ・グラデはPNGが有利すぎる）
        random.seed(1)
        img = Image.new("RGB", (1280, 800))
        px = img.load()
        for y in range(800):
            for x in range(1280):
                px[x, y] = (
                    random.randrange(256),
                    random.randrange(256),
                    random.randrange(256),
                )
        img.save(buf, format="PNG")
        jpeg = OpenAICompatProvider._to_jpeg(buf.getvalue())
        assert len(jpeg) < len(buf.getvalue())
        out = Image.open(io.BytesIO(jpeg))
        assert out.size == (1280, 800)
        assert out.format == "JPEG"

    @pytest.mark.asyncio
    async def test_call_sends_jpeg(self):
        """画像付きリクエストは data:image/jpeg で送る。"""
        from unittest.mock import AsyncMock, MagicMock, patch

        from ai_desktop_agent.agent.llm.openai_compat_provider import (
            OpenAICompatProvider,
        )

        with patch("openai.AsyncOpenAI", autospec=True):
            provider = OpenAICompatProvider(api_key="k")
        choice = MagicMock()
        choice.message.content = '{"ok": true}'
        provider._client.chat.completions.create = AsyncMock(
            return_value=MagicMock(choices=[choice])
        )
        await provider._call("p", image_bytes=b"fakepng")
        kwargs = provider._client.chat.completions.create.call_args.kwargs
        url = kwargs["messages"][1]["content"][1]["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")


class _BlockingLLM(MockLLMProvider):
    """最初の decide でブロックし、外部から解放できるモック。"""

    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.released = asyncio.Event()

    async def decide_next_action(self, *args, **kwargs):
        self._decide_calls.append(args)
        self.entered.set()
        await self.released.wait()
        return _decision(ActionType.SUBTASK_COMPLETE)


class TestCancelAndPause:
    @pytest.mark.asyncio
    async def test_cancel_marks_failed_and_persists(self, tmp_path):
        """キャンセル時は FAILED に倒して保存する（沈黙終了しない）。"""
        import asyncio

        store = TaskStore(root=tmp_path / "data")
        session = TaskSession(llm=_BlockingLLM(), display=FakeDisplayBackend(), store=store)
        task = asyncio.create_task(session.run("キャンセルテスト"))
        await asyncio.wait_for(session.llm.entered.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.loop.state == AgentState.FAILED
        rec = store.load(session.id)
        assert rec is not None
        assert rec.state == "failed"

    @pytest.mark.asyncio
    async def test_pause_then_resume_completes(self):
        """LLM待ち中の pause でも遷移エラーなく完了できる。"""
        import asyncio

        llm = _BlockingLLM()
        session = TaskSession(llm=llm, display=FakeDisplayBackend())
        task = asyncio.create_task(session.run("一時停止テスト"))
        await asyncio.wait_for(llm.entered.wait(), timeout=10)
        # _execute_phase 内（state=EXECUTING）で一時停止
        session.pause()
        assert session.loop.state == AgentState.PAUSED
        await asyncio.sleep(0.2)
        session.resume()
        llm.released.set()
        result = await asyncio.wait_for(task, timeout=30)
        assert result is True


class TestExceptionMarksFailed:
    @pytest.mark.asyncio
    async def test_llm_error_leads_to_failed(self, tmp_path):
        """LLM例外時は FAILED で終わる（executing のまま残さない）。"""

        class _BoomLLM(MockLLMProvider):
            async def decide_next_action(self, *args, **kwargs):
                raise RuntimeError("API down")

        store = TaskStore(root=tmp_path / "data")
        session = TaskSession(llm=_BoomLLM(), display=FakeDisplayBackend(), store=store)
        result = await session.run("例外テスト")
        assert result is False
        assert session.loop.state == AgentState.FAILED
        assert store.load(session.id).state == "failed"


class TestTypeTextShift:
    """type_text の Shift 合成テスト（QEMU 実測ルール）。"""

    @staticmethod
    def _client_with_recorder():
        from ai_desktop_agent.vm.vnc_client import VNCClient

        c = VNCClient()
        calls: list = []

        class _FakeVNC:
            def keyPress(self, key):  # noqa: N802
                calls.append(("press", key))

            def keyDown(self, key):  # noqa: N802
                calls.append(("down", key))

            def keyUp(self, key):  # noqa: N802
                calls.append(("up", key))

        c._client = _FakeVNC()
        c._connected = True
        return c, calls

    def test_shift_punctuation_uses_shift_composition(self):
        from ai_desktop_agent.vm.vnc_client import _SHIFT_PAIRS

        assert _SHIFT_PAIRS[">"] == "."
        assert _SHIFT_PAIRS['"'] == "'"
        assert _SHIFT_PAIRS["?"] == "/"
        # 大文字は素通し（QEMU が直接復元する）
        assert "A" not in _SHIFT_PAIRS
        assert "Z" not in _SHIFT_PAIRS

    def test_type_text_sequence(self):
        c, calls = self._client_with_recorder()
        c.type_text("A>")
        # A は直接、> は Shift 合成
        assert ("press", "A") in calls
        assert ("down", "shift") in calls
        assert ("press", ".") in calls
        assert ("up", "shift") in calls
        assert calls.count(("down", "shift")) == 1

    def test_type_text_newline_and_tab(self):
        c, calls = self._client_with_recorder()
        c.type_text("a\nb\t")
        downs = [k for op, k in calls if op == "down" and k in ("enter", "tab")]
        assert downs == ["enter", "tab"]


class TestKeysymCompletion:
    """vncdotool KEYMAP の補完テスト（ord クラッシュ防止）。"""

    def test_missing_keys_completed(self):
        from vncdotool.client import KEYMAP

        import ai_desktop_agent.vm.vnc_client  # noqa: F401 (補完を実行)

        for name in ("backspace", "escape", "insert", "page_up", "page_down"):
            assert name in KEYMAP, name

    def test_map_key_resolves_all_supported_names(self):
        from ai_desktop_agent.vm.vnc_client import _KEY_MAP, VNCClient

        for name in _KEY_MAP:
            assert isinstance(VNCClient._map_key(name), str)

    def test_key_press_backspace_no_crash(self):
        from ai_desktop_agent.vm.vnc_client import VNCClient

        c = VNCClient()
        calls: list = []

        class _FakeVNC:
            def keyDown(self, key):  # noqa: N802
                # 実 vncdotool と同じ解決をして未登録なら TypeError
                from vncdotool.client import KEYMAP

                calls.append(("down", key))
                if len(key) > 1 and key not in KEYMAP:
                    raise TypeError(f"ord() failed: {key}")

            def keyUp(self, key):  # noqa: N802
                calls.append(("up", key))

        c._client = _FakeVNC()
        c._connected = True
        c.key_press("backspace")
        c.key_press("escape")
        assert ("down", "backspace") in calls
        assert ("down", "escape") in calls


class TestWakeOnBlack:
    """真っ黒画面でのウェイクアップテスト。"""

    @staticmethod
    def _png(color: tuple[int, int, int], size=(16, 12)) -> bytes:
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", size, color).save(buf, format="PNG")
        return buf.getvalue()

    def test_is_black_screen(self):
        from ai_desktop_agent.server.session import _is_black_screen
        from ai_desktop_agent.vm.screenshot import Screenshot

        black = Screenshot(image_bytes=self._png((0, 0, 0)), width=16, height=12)
        white = Screenshot(image_bytes=self._png((255, 255, 255)), width=16, height=12)
        assert _is_black_screen(black) is True
        assert _is_black_screen(white) is False
        broken = Screenshot(image_bytes=b"not-an-image", width=0, height=0)
        assert _is_black_screen(broken) is False

    @pytest.mark.asyncio
    async def test_wake_sends_shift_and_retries(self):
        from ai_desktop_agent.agent.llm.mock import MockLLMProvider
        from ai_desktop_agent.server.session import TaskSession
        from ai_desktop_agent.vm.fake import FakeDisplayBackend
        from ai_desktop_agent.vm.screenshot import Screenshot

        class _BlackThenNormal(FakeDisplayBackend):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def capture_screen(self, *, with_overlay=True):
                self.calls += 1
                color = (0, 0, 0) if self.calls == 1 else (200, 200, 200)
                buf = __import__("io").BytesIO()
                __import__("PIL.Image", fromlist=["Image"]).new("RGB", (16, 12), color).save(
                    buf, format="PNG"
                )
                return Screenshot(image_bytes=buf.getvalue(), width=16, height=12)

        disp = _BlackThenNormal()
        session = TaskSession(llm=MockLLMProvider(), display=disp)
        ss = await session._capture_screenshot()
        assert disp.calls == 2  # 黒→ウェイク→再取得
        assert "shift" in disp.key_presses
        # 返るのは2枚目（明るい方）
        import io

        from PIL import Image

        mean = sum(Image.open(io.BytesIO(ss.image_bytes)).convert("L").tobytes()) / (16 * 12)
        assert mean > 100
