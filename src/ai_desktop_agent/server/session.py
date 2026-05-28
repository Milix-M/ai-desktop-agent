"""タスクセッション — エージェントループの実行を管理する。

AgentLoop + ActionExecutor + LLMProvider を束ね、
1つのタスクを最初から最後まで実行する。
"""

import asyncio
import logging
import os
import uuid
from collections.abc import Callable

from ai_desktop_agent.actions.executor import ActionExecutor
from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.base import LLMProvider
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ActionDecision, ErrorContext
from ai_desktop_agent.agent.loop import AgentLoop
from ai_desktop_agent.agent.state import AgentState, Goal, Subtask
from ai_desktop_agent.vm.base import DisplayBackend
from ai_desktop_agent.vm.fake import FakeDisplayBackend
from ai_desktop_agent.vm.screenshot import Screenshot
from ai_desktop_agent.vm.vnc_client import VNCClient

logger = logging.getLogger(__name__)


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
        self.llm = llm or MockLLMProvider()

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
            self.display = FakeDisplayBackend()

        self.executor = ActionExecutor(self.display)
        self._task: asyncio.Task | None = None

        # イベントコールバック
        self._on_state_change: list[Callable] = []
        self._on_action: list[Callable] = []
        self._on_error: list[Callable] = []
        self._on_complete: list[Callable] = []

    # ── イベント ──────────────────────────────────────

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

    # ── メインループ ──────────────────────────────────

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
                    # 予期しない状態 → 終了
                    break

            success = self.loop.state == AgentState.COMPLETED
            await self._emit_complete(success)
            return success

        except Exception as e:
            logger.exception("タスク実行中にエラー: %s", e)
            await self._emit_error(str(e))
            return False

    async def _execute_phase(self) -> None:
        """EXECUTING フェーズ: LLMに次のアクションを決定させる。"""
        subtask = self.loop.context.current_subtask
        if subtask is None:
            self.loop.recover_failed()
            return

        decision = await self._decide_action(subtask)
        action = decision.action

        # アクション実行
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
            # VERIFYING へ → _verify_phase() へ

        await self._emit_action(action, success)
        await self._emit_state_change()

    async def _verify_phase(self) -> None:
        """VERIFYING フェーズ: アクション結果を検証する。"""
        # 簡易実装: 最後のアクションが成功なら verify_success、失敗なら verify_failed
        if self.loop.context.action_history:
            last = self.loop.context.action_history[-1]
            if last.success:
                self.loop.verify_success()
            else:
                self.loop.verify_failed()
        else:
            self.loop.verify_success()

        await self._emit_state_change()

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

    async def _decide_action(self, subtask: Subtask) -> ActionDecision:
        """LLMに次のアクションを決定させる。画面キャプチャ付き。"""
        screenshot = self._capture_screenshot()
        return await self.llm.decide_next_action(
            goal=self.loop.context.goal or Goal(description=""),
            current_subtask=subtask,
            action_history=self.loop.context.action_history,
            screenshot=screenshot,
        )

    def _capture_screenshot(self) -> Screenshot | None:
        """現在のVM画面をキャプチャする。失敗時は None。"""
        if not self.display.is_connected:
            return None
        try:
            return self.display.capture_screen()
        except Exception:
            logger.debug("画面キャプチャに失敗", exc_info=True)
            return None

    # ── 制御 ──────────────────────────────────────────

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
