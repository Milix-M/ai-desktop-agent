"""タスクセッション — エージェントループの実行を管理する。

AgentLoop + ActionExecutor + LLMProvider を束ね、
1つのタスクを最初から最後まで実行する。

画面には常に座標グリッド＋カーソル位置が重畳される（VNCClientが自動付与）。
region_select による2段階精密クリックをサポートする。
"""

import asyncio
import logging
import os
import uuid
from collections.abc import Callable

from ai_desktop_agent.actions.executor import ActionExecutor
from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.base import LLMProvider
from ai_desktop_agent.agent.llm.factory import create_llm_provider
from ai_desktop_agent.agent.llm.types import ActionDecision, ErrorContext
from ai_desktop_agent.agent.loop import AgentLoop
from ai_desktop_agent.agent.state import AgentState, Goal, Subtask
from ai_desktop_agent.vm.base import DisplayBackend
from ai_desktop_agent.vm.screenshot import Screenshot
from ai_desktop_agent.vm.vnc_client import VNCClient

logger = logging.getLogger(__name__)

MAX_REGION_ZOOM_DEPTH = 3  # region_select の最大入れ子回数


class TaskSession:
    """1つのユーザータスクを管理するセッション。

    AgentLoop の状態遷移を駆動し、各フェーズで LLM を呼び出し、
    アクションを ActionExecutor で実行する。
    """

    def __init__(
        self,
        llm: LLMProvider | None = None,
        display: DisplayBackend | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.loop = AgentLoop()
        self.llm = llm or create_llm_provider()

        if display is not None:
            self.display = display
        elif os.environ.get("VNC_HOST"):
            vnc_host = os.environ["VNC_HOST"]
            vnc_port = int(os.environ.get("VNC_PORT", "5900"))
            vnc_password = os.environ.get("VNC_PASSWORD")
            logger.info("VNC 接続: %s:%d", vnc_host, vnc_port)
            self.display = VNCClient()
            self.display.connect(vnc_host, vnc_port, vnc_password)
        else:
            raise ValueError(
                "VNC_HOST が設定されていません。"
                "環境変数 VNC_HOST を設定するか、display オブジェクトを明示的に渡してください。"
            )

        self.executor = ActionExecutor(self.display)
        self._task: asyncio.Task | None = None

        # イベントコールバック
        self._on_state_change: list[Callable] = []
        self._on_action: list[Callable] = []
        self._on_error: list[Callable] = []
        self._on_complete: list[Callable] = []

    # ── イベント ──────────────────────────────

    def on_state_change(self, cb: Callable) -> None:
        self._on_state_change.append(cb)

    def on_action(self, cb: Callable) -> None:
        self._on_action.append(cb)

    def on_error(self, cb: Callable) -> None:
        self._on_error.append(cb)

    def on_complete(self, cb: Callable) -> None:
        self._on_complete.append(cb)

    async def _emit_state_change(self) -> None:
        for cb in self._on_state_change:
            if asyncio.iscoroutinefunction(cb):
                await cb(self.loop.state, self.loop.context)
            else:
                cb(self.loop.state, self.loop.context)

    async def _emit_action(self, action: Action, success: bool) -> None:
        for cb in self._on_action:
            if asyncio.iscoroutinefunction(cb):
                await cb(action, success)
            else:
                cb(action, success)

    async def _emit_error(self, error: str) -> None:
        for cb in self._on_error:
            if asyncio.iscoroutinefunction(cb):
                await cb(error)
            else:
                cb(error)

    async def _emit_complete(self, success: bool) -> None:
        for cb in self._on_complete:
            if asyncio.iscoroutinefunction(cb):
                await cb(success)
            else:
                cb(success)

    # ── メインループ ───────────────────────────

    async def run(self, instruction: str) -> bool:
        """タスクを最初から最後まで実行する。

        Returns:
            タスクが正常に完了したかどうか。
        """
        goal = Goal(description=instruction)
        self.loop.start(goal)
        await self._emit_state_change()

        try:
            # Phase 1: UNDERSTANDING
            understanding = await self.llm.understand_instruction(goal)
            goal.intent = understanding.intent
            goal.target_application = understanding.target_application
            goal.constraints = understanding.constraints
            self.loop.understanding_done()
            await self._emit_state_change()

            # Phase 2: PLANNING
            decomposition = await self.llm.decompose_task(goal, 0)
            if not decomposition.subtasks:
                self.loop.planning_failed()
                await self._emit_error("タスクの分解に失敗しました")
                return False

            self.loop.plan_ready(decomposition.subtasks)
            await self._emit_state_change()

            # Phase 3-5: EXECUTING → WAITING → VERIFYING (ループ)
            while self.loop.is_running:
                if self.loop.state == AgentState.EXECUTING:
                    await self._execute_phase()

                elif self.loop.state == AgentState.VERIFYING:
                    await self._verify_phase()

                elif self.loop.state == AgentState.RECOVERING:
                    await self._recover_phase()

                elif self.loop.state == AgentState.PAUSED:
                    await asyncio.sleep(0.5)
                    continue

                else:
                    break

            success = self.loop.state == AgentState.COMPLETED
            await self._emit_complete(success)
            return success

        except Exception as e:
            logger.exception("タスク実行中にエラー: %s", e)
            await self._emit_error(str(e))
            return False

    # ── EXECUTING フェーズ ─────────────────────

    async def _execute_phase(self) -> None:
        """EXECUTING フェーズ: LLMに次のアクションを決定させる。

        region_select の場合、拡大表示 → 再問い合わせ → 精密クリックの
        2段階ワークフローを実行する。
        """
        subtask = self.loop.context.current_subtask
        if subtask is None:
            self.loop.recover_failed()
            return

        # 現在の画面を取得（オーバーレイ付き）
        screenshot = self._capture_screenshot()

        # LLM に判断させる
        decision = await self._decide_action(subtask, screenshot)

        # region_select の処理：ズーム → 再判断 → 精密操作
        if decision.action.action_type == ActionType.REGION_SELECT:
            decision = await self._handle_region_zoom(subtask, decision)
            if decision is None:
                # ズーム後も判断できなかった
                self.loop.action_executed()
                self.loop.wait_complete()
                self.loop.verify_failed()
                await self._emit_state_change()
                return

        # アクション実行
        action = decision.action
        logger.info(
            "アクション実行: %s %s (confidence=%.2f)",
            action.action_type.value,
            action.params,
            decision.confidence,
        )
        success = await self.executor.execute(action)
        self.loop.record_action(action, success)

        # 画面変化を待つ
        await asyncio.sleep(0.5)

        # 状態遷移
        if action.action_type == ActionType.SUBTASK_COMPLETE:
            self.loop.action_executed()
            self.loop.wait_complete()
            self.loop.verify_subtask_done()
        else:
            self.loop.action_executed()
            self.loop.wait_complete()

        await self._emit_action(action, success)
        await self._emit_state_change()

    async def _handle_region_zoom(
        self, subtask: Subtask, decision: ActionDecision, depth: int = 0
    ) -> ActionDecision | None:
        """region_select を処理: 領域拡大 → LLM再問い合わせ → 精密アクション。

        Args:
            subtask: 現在のサブタスク。
            decision: region_select を含むアクション決定。
            depth: 再帰の深さ（MAX_REGION_ZOOM_DEPTH で打ち切り）。

        Returns:
            精密なアクション決定、または失敗時は None。
        """
        if depth >= MAX_REGION_ZOOM_DEPTH:
            logger.warning("region_select の最大深度に達しました。フォールバックします。")
            return None

        params = decision.action.params
        rx = int(params.get("x", 0))
        ry = int(params.get("y", 0))
        rw = int(params.get("width", 200))
        rh = int(params.get("height", 200))

        logger.info("領域拡大: (%d, %d) %dx%d", rx, ry, rw, rh)

        # 領域のスクリーンショットを取得（オーバーレイなし、ズーム用）
        raw = self.display.capture_raw()
        zoomed = raw.crop(rx, ry, rw, rh, scale=2.0)

        # 拡大画像を使って LLM に再判断させる
        zoom_decision = await self.llm.decide_next_action(
            goal=self.loop.context.goal or Goal(description=""),
            current_subtask=subtask,
            action_history=self.loop.context.action_history,
            screenshot=zoomed,
            is_zoomed=True,
            zoom_origin=(rx, ry),
        )

        # まだ region_select してきたら再帰
        if zoom_decision.action.action_type == ActionType.REGION_SELECT:
            return await self._handle_region_zoom(subtask, zoom_decision, depth + 1)

        # 相対座標を絶対座標に変換
        if zoom_decision.action.params:
            mapped_params = dict(zoom_decision.action.params)

            for coord_key in ("x", "start_x", "end_x"):
                if coord_key in mapped_params:
                    mapped_params[coord_key] = int(mapped_params[coord_key]) + rx
            for coord_key in ("y", "start_y", "end_y"):
                if coord_key in mapped_params:
                    mapped_params[coord_key] = int(mapped_params[coord_key]) + ry

            zoom_decision = ActionDecision(
                action=Action(
                    action_type=zoom_decision.action.action_type,
                    params=mapped_params,
                ),
                expected_effect=zoom_decision.expected_effect,
                confidence=zoom_decision.confidence,
                reasoning=zoom_decision.reasoning,
            )

        logger.info(
            "精密アクション: %s %s (confidence=%.2f)",
            zoom_decision.action.action_type.value,
            zoom_decision.action.params,
            zoom_decision.confidence,
        )
        return zoom_decision

    # ── VERIFYING フェーズ ─────────────────────

    async def _verify_phase(self) -> None:
        """VERIFYING フェーズ: アクション結果を検証する。

        単純な機械的成功/失敗だけでなく、最後のクリックアクションに
        対しては画面が期待通り変化したかも確認する。
        """
        if self.loop.context.action_history:
            last = self.loop.context.action_history[-1]
            if not last.success:
                # 機械的に失敗 → 即 RECOVERING
                self.loop.verify_failed()
                await self._emit_state_change()
                return

            # クリック系アクションの場合、追加の画面検証を行う
            if last.action.action_type in (
                ActionType.LEFT_CLICK,
                ActionType.RIGHT_CLICK,
                ActionType.DOUBLE_CLICK,
            ):
                verified = await self._verify_click_effect(last)
                if not verified:
                    self.loop.verify_failed()
                    await self._emit_state_change()
                    return

            self.loop.verify_success()
        else:
            self.loop.verify_success()

        await self._emit_state_change()

    async def _verify_click_effect(self, record) -> bool:
        """クリック後に画面が変化したかを LLM に検証させる。"""
        try:
            await asyncio.sleep(0.3)
            after_screenshot = self._capture_screenshot()

            # TODO: LLMに前後比較をさせる（現状は簡易実装として常に成功扱い）
            # 将来的には before/after の両方を LLM に送り、変化を検証させる
            _ = after_screenshot
            return True
        except Exception:
            logger.exception("クリック検証中にエラー")
            return False

    # ── RECOVERING フェーズ ────────────────────

    async def _recover_phase(self) -> None:
        """RECOVERING フェーズ: エラーからの回復。"""
        subtask = self.loop.context.current_subtask
        if subtask is None:
            self.loop.recover_failed()
            return

        # 最後の失敗アクションを取得
        last_error = None
        for record in reversed(self.loop.context.action_history):
            if not record.success:
                last_error = ErrorContext(
                    action=record.action,
                    error_message=record.error_message,
                    retry_count=self.loop.context.retry_counts.get(
                        f"{subtask.id}:{record.action.action_type.value}", 0
                    ),
                )
                break

        if last_error is None:
            self.loop.recover_retry()
            return

        plan = await self.llm.recover_from_error(
            last_error,
            self.loop.context.action_history,
            subtask,
        )

        if plan.recoverable:
            self.loop.recover_retry()
        else:
            self.loop.recover_failed()

        await self._emit_state_change()

    # ── アクション決定 ─────────────────────────

    async def _decide_action(
        self,
        subtask: Subtask,
        screenshot: Screenshot,
        error_context: ErrorContext | None = None,
    ) -> ActionDecision:
        """LLM に次のアクションを決定させる（画面キャプチャ付き）。"""
        return await self.llm.decide_next_action(
            goal=self.loop.context.goal or Goal(description=""),
            current_subtask=subtask,
            action_history=self.loop.context.action_history,
            screenshot=screenshot,
            error_context=error_context,
        )

    def _capture_screenshot(self) -> Screenshot:
        """現在のVM画面をキャプチャする（オーバーレイ付き）。"""
        if not self.display.is_connected:
            raise RuntimeError("ディスプレイが接続されていません")
        return self.display.capture_screen()

    # ── 制御 ────────────────────────────────────

    async def start_async(self, instruction: str) -> None:
        """バックグラウンドでタスクを開始する。"""
        if self._task and not self._task.done():
            raise RuntimeError("タスクは既に実行中です")
        self._task = asyncio.create_task(self.run(instruction))

    def pause(self) -> None:
        if self.loop.state == AgentState.EXECUTING:
            self.loop.pause()

    def resume(self) -> None:
        if self.loop.state == AgentState.PAUSED:
            self.loop.resume()

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()
