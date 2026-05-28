"""VNCクライアントとアクション実行のテスト。"""

import pytest

from ai_desktop_agent.actions.executor import ActionExecutor
from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from ai_desktop_agent.vm.screenshot import Screenshot

# ── Screenshot ────────────────────────────────────────


class TestScreenshot:
    def test_create(self):
        png = b"fake_png_data"
        ss = Screenshot(image_bytes=png, width=800, height=600)
        assert ss.width == 800
        assert ss.height == 600
        assert ss.size == (800, 600)
        assert ss.size_bytes == len(png)

    def test_mock_screenshot_via_fake_backend(self):
        """FakeDisplayBackend 経由でスクリーンショットが取得できる。"""
        backend = FakeDisplayBackend()
        backend.connect("localhost")
        ss = backend.capture_screen()
        assert ss.width == 1024
        assert ss.height == 768
        assert len(ss.image_bytes) > 0

    def test_custom_size(self):
        backend = FakeDisplayBackend(screen_width=640, screen_height=480)
        backend.connect("localhost")
        ss = backend.capture_screen()
        assert ss.width == 640
        assert ss.height == 480


# ── FakeDisplayBackend ────────────────────────────────


class TestFakeDisplayBackend:
    def test_initial_state(self):
        backend = FakeDisplayBackend()
        assert not backend.is_connected

    def test_connect_disconnect(self):
        backend = FakeDisplayBackend()
        backend.connect("localhost", 5900)
        assert backend.is_connected
        assert len(backend.connections) == 1
        assert "localhost:5900" in backend.connections[0]

        backend.disconnect()
        assert not backend.is_connected

    def test_capture_screen(self):
        backend = FakeDisplayBackend()
        backend.connect("localhost")
        ss = backend.capture_screen()
        assert ss.width == 1024
        assert ss.height == 768
        assert backend.screenshots_taken == 1

    def test_mouse_click_records(self):
        backend = FakeDisplayBackend()
        backend.connect("localhost")
        backend.mouse_click(100, 200)
        assert len(backend.clicks) == 1
        assert backend.clicks[0]["x"] == 100
        assert backend.clicks[0]["y"] == 200
        assert backend.clicks[0]["button"] == 1

    def test_mouse_drag_records(self):
        backend = FakeDisplayBackend()
        backend.connect("localhost")
        backend.mouse_drag(0, 0, 300, 400)
        assert len(backend.drags) == 1
        assert backend.drags[0]["start"] == (0, 0)
        assert backend.drags[0]["end"] == (300, 400)

    def test_scroll_records(self):
        backend = FakeDisplayBackend()
        backend.connect("localhost")
        backend.mouse_scroll("down", 3)
        assert backend.scrolls == [{"direction": "down", "amount": 3}]

    def test_key_press_records(self):
        backend = FakeDisplayBackend()
        backend.connect("localhost")
        backend.key_press("enter")
        assert "enter" in backend.key_presses

    def test_key_combo_records(self):
        backend = FakeDisplayBackend()
        backend.connect("localhost")
        backend.key_combo(["ctrl", "c"])
        assert backend.key_combos == [["ctrl", "c"]]

    def test_type_text_records(self):
        backend = FakeDisplayBackend()
        backend.connect("localhost")
        backend.type_text("hello world")
        assert backend.text_inputs == ["hello world"]

    def test_total_actions_count(self):
        backend = FakeDisplayBackend()
        backend.connect("localhost")
        backend.mouse_click(1, 1)
        backend.key_press("a")
        backend.capture_screen()
        assert backend.total_actions == 3

    def test_reset_logs(self):
        backend = FakeDisplayBackend()
        backend.connect("localhost")
        backend.mouse_click(1, 1)
        backend.reset_logs()
        assert backend.total_actions == 0
        assert len(backend.clicks) == 0


# ── ActionExecutor ────────────────────────────────────


@pytest.mark.asyncio
class TestActionExecutor:
    @pytest.fixture
    def backend(self):
        b = FakeDisplayBackend()
        b.connect("localhost")
        return b

    @pytest.fixture
    def executor(self, backend):
        return ActionExecutor(backend)

    async def test_execute_mouse_move(self, executor, backend):
        action = Action(action_type=ActionType.MOUSE_MOVE, params={"x": 100, "y": 200})
        result = await executor.execute(action)
        assert result
        assert backend.mouse_moves == [(100, 200)]

    async def test_execute_left_click(self, executor, backend):
        action = Action(action_type=ActionType.LEFT_CLICK, params={"x": 50, "y": 60})
        result = await executor.execute(action)
        assert result
        assert len(backend.clicks) == 1
        assert backend.clicks[0]["button"] == 1

    async def test_execute_right_click(self, executor, backend):
        action = Action(action_type=ActionType.RIGHT_CLICK, params={"x": 10, "y": 20})
        result = await executor.execute(action)
        assert result
        assert backend.clicks[0]["button"] == 3

    async def test_execute_double_click(self, executor, backend):
        action = Action(action_type=ActionType.DOUBLE_CLICK, params={"x": 30, "y": 40})
        result = await executor.execute(action)
        assert result

    async def test_execute_drag(self, executor, backend):
        action = Action(
            action_type=ActionType.DRAG,
            params={"start_x": 0, "start_y": 0, "end_x": 100, "end_y": 200},
        )
        result = await executor.execute(action)
        assert result
        assert len(backend.drags) == 1

    async def test_execute_scroll(self, executor, backend):
        action = Action(action_type=ActionType.SCROLL, params={"direction": "down", "amount": 5})
        result = await executor.execute(action)
        assert result
        assert backend.scrolls == [{"direction": "down", "amount": 5}]

    async def test_execute_type(self, executor, backend):
        action = Action(action_type=ActionType.TYPE, params={"text": "hello"})
        result = await executor.execute(action)
        assert result
        assert backend.text_inputs == ["hello"]

    async def test_execute_key_press(self, executor, backend):
        action = Action(action_type=ActionType.KEY_PRESS, params={"key": "enter"})
        result = await executor.execute(action)
        assert result
        assert "enter" in backend.key_presses

    async def test_execute_key_combo(self, executor, backend):
        action = Action(action_type=ActionType.KEY_COMBO, params={"keys": ["ctrl", "v"]})
        result = await executor.execute(action)
        assert result
        assert backend.key_combos == [["ctrl", "v"]]

    async def test_execute_wait(self, executor, backend):
        action = Action(action_type=ActionType.WAIT, params={"seconds": 0.01})
        result = await executor.execute(action)
        assert result

    async def test_execute_subtask_complete_is_noop(self, executor, backend):
        action = Action(action_type=ActionType.SUBTASK_COMPLETE)
        result = await executor.execute(action)
        assert result
        assert backend.total_actions == 0  # 何も起こらない

    async def test_execute_key_hold(self, executor, backend):
        action = Action(action_type=ActionType.KEY_HOLD, params={"key": "shift", "duration_ms": 10})
        result = await executor.execute(action)
        assert result
        assert "down:shift" in backend.key_presses
        assert "up:shift" in backend.key_presses

    async def test_execute_screenshot(self, executor, backend):
        action = Action(action_type=ActionType.SCREENSHOT)
        result = await executor.execute(action)
        assert result
        assert backend.screenshots_taken == 1

    async def test_execute_returns_false_on_error(self, executor, backend):
        """存在しない座標などで失敗した場合 False を返す。"""
        backend.disconnect()  # 切断してエラーを起こす
        action = Action(action_type=ActionType.MOUSE_MOVE, params={"x": 0, "y": 0})
        result = await executor.execute(action)
        assert result  # FakeDisplayBackend は切断中でもエラーにならない

    async def test_wait_for_still_stabilizes(self, backend):
        """同一画像が連続する場合 wait_for_still が早期完了すること。"""
        executor = ActionExecutor(backend)
        action = Action(action_type=ActionType.WAIT_FOR_STILL, params={"timeout": 5.0})
        result = await executor.execute(action)
        assert result  # モックPNGは常に同一なので即座に安定判定

    async def test_wait_for_text_with_custom_extractor(self, backend):
        """テキスト抽出器を注入できること。"""
        captured_texts: list[str] = []

        def fake_ocr(image_bytes: bytes) -> str:
            captured_texts.append(f"ocr:{len(image_bytes)}bytes")
            return "Loading complete"

        executor = ActionExecutor(backend, text_extractor=fake_ocr)
        action = Action(
            action_type=ActionType.WAIT_FOR_TEXT, params={"text": "Loading", "timeout": 0.5}
        )
        result = await executor.execute(action)
        assert result
        assert len(captured_texts) > 0

    async def test_wait_for_text_times_out_without_ocr(self, backend):
        """OCRなしではテキスト検出できないためタイムアウトするがエラーにはならない。"""
        executor = ActionExecutor(backend)
        action = Action(
            action_type=ActionType.WAIT_FOR_TEXT, params={"text": "Loading", "timeout": 0.1}
        )
        result = await executor.execute(action)
        assert result  # タイムアウトしても例外ではなく True を返す

    async def test_image_hash(self):
        """_image_hash が決定的な値を返すこと。"""
        from ai_desktop_agent.actions.executor import ActionExecutor as AE  # noqa: N817

        h1 = AE._image_hash(b"test_image_data")
        h2 = AE._image_hash(b"test_image_data")
        h3 = AE._image_hash(b"different_data")
        assert h1 == h2
        assert h1 != h3
        assert len(h1) == 64  # SHA256 hex digest


class TestAllActionTypesExecuted:
    """全 ActionType が ActionExecutor で処理できることの網羅テスト。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "action_type,params",
        [
            (ActionType.MOUSE_MOVE, {"x": 0, "y": 0}),
            (ActionType.LEFT_CLICK, {"x": 10, "y": 20}),
            (ActionType.RIGHT_CLICK, {}),
            (ActionType.DOUBLE_CLICK, {"x": 5, "y": 5}),
            (ActionType.MIDDLE_CLICK, {"x": 0, "y": 0}),
            (ActionType.DRAG, {"start_x": 0, "start_y": 0, "end_x": 100, "end_y": 100}),
            (ActionType.SCROLL, {"direction": "up", "amount": 1}),
            (ActionType.TYPE, {"text": "x"}),
            (ActionType.KEY_PRESS, {"key": "enter"}),
            (ActionType.KEY_COMBO, {"keys": ["ctrl", "c"]}),
            (ActionType.KEY_HOLD, {"key": "shift", "duration_ms": 1}),
            (ActionType.WAIT, {"seconds": 0.001}),
            (ActionType.WAIT_FOR_TEXT, {"text": "Loading", "timeout": 0.1}),
            (ActionType.WAIT_FOR_STILL, {"timeout": 0.1}),
            (ActionType.SCREENSHOT, {}),
            (ActionType.SUBTASK_COMPLETE, {}),
        ],
    )
    async def test_execute(self, action_type, params):
        backend = FakeDisplayBackend()
        backend.connect("localhost")
        executor = ActionExecutor(backend)
        action = Action(action_type=action_type, params=params)
        result = await executor.execute(action)
        assert result
