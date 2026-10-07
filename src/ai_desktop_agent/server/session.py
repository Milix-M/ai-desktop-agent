"""タスクセッション — エージェントループの実行を管理する。

AgentLoop + ActionExecutor + LLMProvider を束ね、
1つのタスクを最初から最後まで実行する。

画面には常に座標グリッド＋カーソル位置が重畳される（VNCClientが自動付与）。
region_select による2段階精密クリックをサポートする。
"""

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Callable

from ai_desktop_agent.actions.executor import ActionExecutor
from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.base import LLMProvider
from ai_desktop_agent.agent.llm.factory import create_llm_provider
from ai_desktop_agent.agent.llm.types import ActionDecision, ErrorContext
from ai_desktop_agent.agent.loop import AgentLoop
from ai_desktop_agent.agent.state import AgentState, Goal, Subtask
from ai_desktop_agent.server.store import StoredAction, TaskRecord, TaskStore
from ai_desktop_agent.vm.base import DisplayBackend
from ai_desktop_agent.vm.screenshot import Screenshot
from ai_desktop_agent.vm.vnc_client import VNCClient

logger = logging.getLogger(__name__)

#: 真っ黒判定の輝度しきい値（DPMS 消灯時は全画素 0）
BLACK_SCREEN_THRESHOLD = 2.0


def _is_black_screen(screenshot: Screenshot, threshold: float = BLACK_SCREEN_THRESHOLD) -> bool:
    """スクリーンショットがほぼ真っ黒かどうかを返す。"""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(screenshot.image_bytes)) as img:
            raw = img.convert("L").tobytes()
        if not raw:
            return False
        return sum(raw) / len(raw) < threshold
    except Exception:
        logger.debug("黒画面判定に失敗", exc_info=True)
        return False


MAX_REGION_ZOOM_DEPTH = 1  # region_select の入れ子上限（深追い防止）
LOW_CONFIDENCE_THRESHOLD = 0.7  # これ未満のクリックは拡大へ回す
REPEAT_RADIUS_PX = 8  # 同一座標とみなす半径（連続クリック防止用）
ZOOM_SCALE = 2.0
ZOOM_AUTO_SIZE = 240  # 低確信度時の自動拡大サイズ
ZOOM_MIN_SIZE = 50
ZOOM_MAX_SIZE = 500
MAX_ACTIONS_PER_TASK = 200  # 1タスクの上限（トークン燃費対策）
MAX_EMPTY_COMPLETE_REFUSALS = 2  # 操作なし完了宣言の却下回数


class TaskSession:
    """1つのユーザータスクを管理するセッション。

    AgentLoop の状態遷移を駆動し、各フェーズで LLM を呼び出し、
    アクションを ActionExecutor で実行する。
    """

    def __init__(
        self,
        llm: LLMProvider | None = None,
        display: DisplayBackend | None = None,
        store: TaskStore | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.loop = AgentLoop()
        self.llm = llm or create_llm_provider()
        self._store = store
        self._instruction = ""
        self._created_at = time.time()

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
        # 検証用：実行直前の生画像（オーバーレイなし）
        self._last_before_raw: Screenshot | None = None
        # サブタスクごとの空完了却下回数
        self._empty_complete_refusals: dict[str, int] = {}
        # サブタスクごとの検証失敗回数
        self._verify_failures: dict[str, int] = {}
        # 回復・再決定に引き継ぐエラー情報
        self._pending_error: ErrorContext | None = None

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
        self._persist()

    async def _emit_action(self, action: Action, success: bool) -> None:
        for cb in self._on_action:
            if asyncio.iscoroutinefunction(cb):
                await cb(action, success)
            else:
                cb(action, success)
        self._persist()

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
        self._instruction = instruction
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
            # PAUSED 中もループに留まる（一時停止後に復帰できるように）
            while self.loop.is_running or self.loop.state == AgentState.PAUSED:
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
            self._persist(success=success)
            return success

        except asyncio.CancelledError:
            # stop() による中断：状態を FAILED に倒して保存（沈黙終了を防ぐ）
            import contextlib

            logger.warning("タスクがキャンセルされました")
            with contextlib.suppress(ValueError):
                self.loop.fail_now()
            await self._emit_state_change()
            self._persist(success=False)
            raise
        except Exception as e:
            logger.exception("タスク実行中にエラー: %s", e)
            await self._emit_error(str(e))
            # 実行中状態のまま残さないよう FAILED に倒す
            import contextlib

            with contextlib.suppress(ValueError):
                self.loop.fail_now()
            await self._emit_state_change()
            self._persist(success=False)
            return False

    # ── 永続化 ────────────────────────────────────

    def snapshot(self, success: bool | None = None) -> TaskRecord:
        """現在のコンテキストを永続化用レコードに変換する。"""
        ctx = self.loop.context
        goal = ctx.goal
        return TaskRecord(
            id=self.id,
            instruction=self._instruction,
            state=self.loop.state.value,
            success=success,
            actions=[
                StoredAction(
                    action_type=r.action.action_type.value,
                    params=dict(r.action.params or {}),
                    description=r.action.description,
                    success=r.success,
                    error_message=r.error_message,
                    duration_ms=r.duration_ms,
                    at=self._created_at,
                    reasoning=r.reasoning,
                    confidence=r.confidence,
                )
                for r in ctx.action_history
            ],
            subtasks=[
                {
                    "id": s.id,
                    "description": s.description,
                    "expected_outcome": s.expected_outcome,
                }
                for s in ctx.subtasks
            ],
            goal=(
                {
                    "description": goal.description,
                    "intent": goal.intent,
                    "target_application": goal.target_application,
                    "constraints": list(goal.constraints),
                }
                if goal
                else {}
            ),
            created_at=self._created_at,
        )

    def _persist(self, success: bool | None = None) -> None:
        """store があれば現在の状態を保存する（失敗しても継続）。"""
        if self._store is None:
            return
        try:
            self._store.save(self.snapshot(success=success))
        except Exception:
            logger.exception("タスク記録の保存に失敗")

    # ── EXECUTING フェーズ ─────────────────────

    async def _execute_phase(self) -> None:
        """EXECUTING フェーズ: LLMに次のアクションを決定させる。

        region_select の場合、拡大表示 → 再問い合わせ → 精密クリックの
        2段階ワークフローを実行する。低確信度クリックや同一座標の連続
        クリックは自動で拡大へ回して誤クリックを防ぐ。
        """
        subtask = self.loop.context.current_subtask
        if subtask is None:
            self.loop.recover_failed()
            return

        # ステップ上限（トークン燃費対策）
        if len(self.loop.context.action_history) >= MAX_ACTIONS_PER_TASK:
            logger.warning("アクション上限に到達したため中断します")
            await self._emit_error("アクション上限に到達しました")
            self.loop.fail_now()
            await self._emit_state_change()
            return

        # 現在の画面を取得（オーバーレイ付き）
        screenshot = await self._capture_screenshot()

        # LLM に判断させる（手抜き完了は最大2回まで却下して再問い合わせ）。
        # 回復フローからのエラー情報があれば引き継ぐ。
        error_ctx = self._pending_error
        self._pending_error = None
        decision = await self._decide_action(subtask, screenshot, error_ctx)
        for _ in range(MAX_EMPTY_COMPLETE_REFUSALS):
            if not self._is_empty_complete(decision):
                break
            self._empty_complete_refusals[subtask.id] = (
                self._empty_complete_refusals.get(subtask.id, 0) + 1
            )
            logger.info(
                "操作なしの完了宣言を却下（%d回目）: %s",
                self._empty_complete_refusals[subtask.id],
                decision.reasoning,
            )
            error_ctx = ErrorContext(
                action=decision.action,
                error_message=(
                    "このサブタスクではまだ何も操作していません。"
                    "完了宣言の前に、画面を見て具体的な操作"
                    "（クリック・入力・キー操作・領域拡大）を1つ以上実行してください。"
                ),
                retry_count=self._empty_complete_refusals[subtask.id],
            )
            decision = await self._decide_action(subtask, screenshot, error_ctx)
        else:
            # 3回連続で完了宣言なら受け入れる（本当に何もない場合の脱出）
            pass

        # 低確信度クリックは拡大へ回す（いきなり撃たせない）
        if (
            decision.action.action_type
            in (
                ActionType.LEFT_CLICK,
                ActionType.RIGHT_CLICK,
                ActionType.DOUBLE_CLICK,
                ActionType.MIDDLE_CLICK,
            )
            and decision.confidence < LOW_CONFIDENCE_THRESHOLD
        ):
            logger.info(
                "低確信度 (%.2f) のため拡大へ回します: %s",
                decision.confidence,
                decision.action.params,
            )
            region_decision = self._region_around(decision.action.params, screenshot)
            decision = await self._handle_region_zoom(subtask, region_decision)
            if decision is None:
                self.loop.action_executed()
                self.loop.wait_complete()
                self.loop.verify_failed()
                await self._emit_state_change()
                return
        # region_select の処理：ズーム → 再判断 → 精密操作
        elif decision.action.action_type == ActionType.REGION_SELECT:
            decision = await self._handle_region_zoom(subtask, decision)
            if decision is None:
                # ズーム後も判断できなかった
                self.loop.action_executed()
                self.loop.wait_complete()
                self.loop.verify_failed()
                await self._emit_state_change()
                return
        # 同一座標の連続クリックは拡大へ回す（無限ループ防止）
        elif self._is_repeat_click(decision.action):
            logger.info("同一座標の連続クリックを検出、拡大へ回します: %s", decision.action.params)
            region_decision = self._region_around(decision.action.params, screenshot)
            zoomed = await self._handle_region_zoom(subtask, region_decision)
            if zoomed is not None:
                decision = zoomed

        # 画面内にクランプ（範囲外座標のズレを防ぐ）
        decision = self._clamp_decision(decision, screenshot)

        # 一時停止中は再開まで待つ（遷移エラーを防ぐ）
        await self._wait_if_paused()

        # アクション実行
        action = decision.action
        logger.info(
            "アクション実行: %s %s (confidence=%.2f)",
            action.action_type.value,
            action.params,
            decision.confidence,
        )
        # 検証用に実行直前の生画像を保持（オーバーレイの影響を除外）
        try:
            capture_raw = getattr(self.display, "capture_raw", None)
            self._last_before_raw = capture_raw() if callable(capture_raw) else None
        except Exception:
            self._last_before_raw = None
        success = await self.executor.execute(action)
        self.loop.record_action(
            action,
            success,
            reasoning=decision.reasoning,
            confidence=decision.confidence,
        )

        # 画面変化を待つ
        await asyncio.sleep(0.5)

        # 一時停止中は再開まで待つ（遷移エラーを防ぐ）
        await self._wait_if_paused()

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

    async def _wait_if_paused(self) -> None:
        """PAUSED の間は再開まで待機する。"""
        while self.loop.state == AgentState.PAUSED:
            await asyncio.sleep(0.5)

    async def _handle_region_zoom(
        self, subtask: Subtask, decision: ActionDecision, depth: int = 0
    ) -> ActionDecision | None:
        """region_select を処理: 領域拡大 → LLM再問い合わせ → 精密アクション。

        座標逆変換は ``absolute = origin + zoomed_coord / total_scale`` で行う。
        入れ子の region_select にも対応できるよう、原点と累積倍率を追跡する。
        拡大画像には相対座標グリッドを重畳してLLMの読み取りを助ける。

        Args:
            subtask: 現在のサブタスク。
            decision: region_select を含むアクション決定。
            depth: 再帰の深さ（MAX_REGION_ZOOM_DEPTH で打ち切り）。

        Returns:
            精密なアクション決定、または失敗時は None。
        """
        del depth  # 反復ループで深さを管理するため未使用
        try:
            raw = self.display.capture_raw()
        except Exception:
            logger.exception("拡大用の生画像取得に失敗")
            return None

        abs_origin_x, abs_origin_y = 0, 0
        total_scale = 1.0
        pending = decision

        for level in range(MAX_REGION_ZOOM_DEPTH + 1):
            if pending.action.action_type != ActionType.REGION_SELECT:
                break
            params = pending.action.params or {}
            try:
                rx = int(params.get("x", 0))
                ry = int(params.get("y", 0))
                rw = int(params.get("width", ZOOM_AUTO_SIZE))
                rh = int(params.get("height", ZOOM_AUTO_SIZE))
            except (TypeError, ValueError):
                logger.warning("不正な region_select パラメータ: %s", params)
                return None

            # 現在画像座標系 → 絶対座標系へ変換
            abs_x = abs_origin_x + int(rx / total_scale) if total_scale else rx
            abs_y = abs_origin_y + int(ry / total_scale) if total_scale else ry
            # 幅・高さも現在の倍率で割って絶対サイズに戻す
            abs_w = max(ZOOM_MIN_SIZE, min(int(rw / total_scale), ZOOM_MAX_SIZE))
            abs_h = max(ZOOM_MIN_SIZE, min(int(rh / total_scale), ZOOM_MAX_SIZE))
            # 画面内にクリップ
            abs_x = max(0, min(abs_x, raw.width - 1))
            abs_y = max(0, min(abs_y, raw.height - 1))
            abs_w = min(abs_w, raw.width - abs_x)
            abs_h = min(abs_h, raw.height - abs_y)

            logger.info(
                "領域拡大 Lv%d: abs=(%d, %d) %dx%d (要求: %s)",
                level,
                abs_x,
                abs_y,
                abs_w,
                abs_h,
                params,
            )

            zoomed, (_ox, _oy, actual_scale) = raw.crop_with_meta(
                abs_x, abs_y, abs_w, abs_h, scale=ZOOM_SCALE
            )
            # 拡大画像に相対グリッドを重畳（細かめ25pxで精度向上）
            try:
                zoomed = zoomed.with_overlay(grid_spacing=25)
            except TypeError:
                zoomed = zoomed.with_overlay()

            abs_origin_x, abs_origin_y = abs_x, abs_y
            total_scale = total_scale * actual_scale

            # 拡大画像を使って LLM に再判断させる
            zoom_decision = await self.llm.decide_next_action(
                goal=self.loop.context.goal or Goal(description=""),
                current_subtask=subtask,
                action_history=self.loop.context.action_history,
                screenshot=zoomed,
                is_zoomed=True,
                zoom_origin=(abs_origin_x, abs_origin_y),
                zoom_scale=total_scale,
            )
            pending = zoom_decision
        else:
            logger.warning("region_select の最大深度に達しました。フォールバックします。")
            return None

        if pending.action.action_type == ActionType.REGION_SELECT:
            logger.warning("ズーム後も region_select が返されました")
            return None

        # 相対座標を絶対座標に変換（total_scale で割るのが要点）
        if pending.action.params:
            mapped_params = dict(pending.action.params)

            for coord_key in ("x", "start_x", "end_x"):
                if coord_key in mapped_params:
                    try:
                        rel = int(mapped_params[coord_key])
                    except (TypeError, ValueError):
                        continue
                    mapped_params[coord_key] = abs_origin_x + int(rel / total_scale)
            for coord_key in ("y", "start_y", "end_y"):
                if coord_key in mapped_params:
                    try:
                        rel = int(mapped_params[coord_key])
                    except (TypeError, ValueError):
                        continue
                    mapped_params[coord_key] = abs_origin_y + int(rel / total_scale)

            # 画面内にクランプ
            mapped_params = self._clamp_params_to_raw(mapped_params, raw)

            try:
                pending = ActionDecision(
                    action=Action(
                        action_type=pending.action.action_type,
                        params=mapped_params,
                    ),
                    expected_effect=pending.expected_effect,
                    confidence=pending.confidence,
                    reasoning=pending.reasoning,
                )
            except ValueError:
                logger.exception("精密アクションの構築に失敗: %s", mapped_params)
                return None

        logger.info(
            "精密アクション: %s %s (confidence=%.2f, scale=%.2f)",
            pending.action.action_type.value,
            pending.action.params,
            pending.confidence,
            total_scale,
        )
        return pending

    # ── 精度ガード ──────────────────────────────────

    @staticmethod
    def _clamp_params_to_raw(params: dict, raw: Screenshot) -> dict:
        """座標パラメータを生画像サイズ内にクランプする。"""
        import contextlib

        out = dict(params)
        w, h = raw.width, raw.height
        for k in ("x", "start_x", "end_x"):
            if k in out:
                with contextlib.suppress(TypeError, ValueError):
                    out[k] = max(0, min(int(out[k]), max(0, w - 1)))
        for k in ("y", "start_y", "end_y"):
            if k in out:
                with contextlib.suppress(TypeError, ValueError):
                    out[k] = max(0, min(int(out[k]), max(0, h - 1)))
        return out

    def _clamp_decision(self, decision: ActionDecision, screenshot: Screenshot) -> ActionDecision:
        """決定の座標を画面内にクランプする（範囲外撃ち防止）。"""
        if not decision.action.params:
            return decision
        clamped = self._clamp_params_to_raw(dict(decision.action.params), screenshot)
        if clamped == decision.action.params:
            return decision
        try:
            return ActionDecision(
                action=Action(action_type=decision.action.action_type, params=clamped),
                expected_effect=decision.expected_effect,
                confidence=decision.confidence,
                reasoning=decision.reasoning,
            )
        except ValueError:
            return decision

    def _region_around(self, params: dict | None, screenshot: Screenshot) -> ActionDecision:
        """指定座標を中心とする region_select 決定を合成する。"""
        params = params or {}
        try:
            cx = int(params.get("x", screenshot.width // 2))
            cy = int(params.get("y", screenshot.height // 2))
        except (TypeError, ValueError):
            cx, cy = screenshot.width // 2, screenshot.height // 2
        size = ZOOM_AUTO_SIZE
        rx = max(0, min(cx - size // 2, max(0, screenshot.width - size)))
        ry = max(0, min(cy - size // 2, max(0, screenshot.height - size)))
        rw = min(size, screenshot.width - rx)
        rh = min(size, screenshot.height - ry)
        return ActionDecision(
            action=Action(
                action_type=ActionType.REGION_SELECT,
                params={"x": rx, "y": ry, "width": rw, "height": rh},
            ),
            expected_effect=f"({cx}, {cy}) 周辺を拡大して精密化",
            confidence=1.0,
            reasoning="低確信度/連続クリックのため自動拡大",
        )

    def _is_empty_complete(self, decision: ActionDecision) -> bool:
        """操作なしの完了宣言かどうか。

        現在のサブタスク開始後に1つも実操作がなく subtask_complete が
        来たら手抜きとみなす（却下して再問い合わせするため）。
        """
        if decision.action.action_type != ActionType.SUBTASK_COMPLETE:
            return False
        return not any(
            r.action.action_type != ActionType.SUBTASK_COMPLETE
            for r in self._actions_since_subtask_start()
        )

    def _actions_since_subtask_start(self) -> list:
        """現在のサブタスク開始後のアクション履歴を返す。"""
        history = self.loop.context.action_history
        for i in range(len(history) - 1, -1, -1):
            if history[i].action.action_type == ActionType.SUBTASK_COMPLETE:
                return history[i + 1 :]
        return list(history)

    def _is_repeat_click(self, action: Action) -> bool:
        """直前と同じクリックの繰り返しかを判定する。"""
        if action.action_type not in (
            ActionType.LEFT_CLICK,
            ActionType.RIGHT_CLICK,
            ActionType.DOUBLE_CLICK,
            ActionType.MIDDLE_CLICK,
        ):
            return False
        params = action.params or {}
        if "x" not in params or "y" not in params:
            return False
        try:
            x, y = int(params["x"]), int(params["y"])
        except (TypeError, ValueError):
            return False
        for record in reversed(self.loop.context.action_history[-3:]):
            rp = record.action.params or {}
            if record.action.action_type != action.action_type:
                continue
            if "x" not in rp or "y" not in rp:
                continue
            try:
                px, py = int(rp["x"]), int(rp["y"])
            except (TypeError, ValueError):
                continue
            if abs(px - x) <= REPEAT_RADIUS_PX and abs(py - y) <= REPEAT_RADIUS_PX:
                return True
        return False

    # ── VERIFYING フェーズ ─────────────────────

    async def _verify_phase(self) -> None:
        """VERIFYING フェーズ: アクション結果を検証する。

        - 機械的失敗 → RECOVERING
        - クリック系 → 画面変化の有無を確認
        - subtask_complete → 期待結果の達成をLLMに画像判定させる
        """
        if not self.loop.context.action_history:
            self.loop.verify_success()
            await self._emit_state_change()
            return

        last = self.loop.context.action_history[-1]
        if not last.success:
            # 機械的に失敗 → 即 RECOVERING
            self._pending_error = ErrorContext(
                action=last.action,
                error_message=last.error_message or "アクションの実行に失敗",
                retry_count=0,
            )
            self.loop.verify_failed()
            await self._emit_state_change()
            return

        # 完了宣言の場合、期待結果の達成を画像で検証する
        if last.action.action_type == ActionType.SUBTASK_COMPLETE:
            await self._verify_subtask_phase(last)
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
        await self._emit_state_change()

    async def _verify_subtask_phase(self, record) -> None:
        """subtask_complete の妥当性をLLMに画像判定させる。

        達成なら次へ、未達なら回復へ。上限超過なら FAILED で打ち切る。
        """
        subtask = self.loop.context.current_subtask
        if subtask is None:
            self.loop.recover_failed()
            await self._emit_state_change()
            return

        try:
            screenshot = await self._capture_screenshot()
            decision = ActionDecision(
                action=record.action,
                expected_effect="",
                confidence=record.confidence,
                reasoning=record.reasoning,
            )
            result = await self.llm.verify_result(
                decision,
                subtask.expected_outcome,
                screenshot=screenshot,
                expected_outcome=subtask.expected_outcome,
            )
        except Exception:
            logger.exception("サブタスク検証中にエラー")
            self.loop.verify_failed()
            await self._emit_state_change()
            return

        if result.success:
            logger.info("サブタスク達成を確認: %s", subtask.id)
            self._pending_error = None
            self.loop.verify_subtask_done()
            await self._emit_state_change()
            return

        fails = self._verify_failures.get(subtask.id, 0) + 1
        self._verify_failures[subtask.id] = fails
        logger.warning(
            "サブタスク未達 (%d/%d): %s — %s",
            fails,
            subtask.max_retries,
            subtask.id,
            result.reasoning,
        )
        if fails > subtask.max_retries:
            await self._emit_error(f"サブタスク未達のため中断: {subtask.description}")
            self.loop.fail_now()
            await self._emit_state_change()
            return

        self._pending_error = ErrorContext(
            action=record.action,
            error_message=(
                f"サブタスクの期待結果を確認できません: {subtask.expected_outcome}。"
                f"検証結果: {result.reasoning} {result.evidence}"
            ),
            retry_count=fails,
        )
        self.loop.verify_failed()
        await self._emit_state_change()

    async def _verify_click_effect(self, record) -> bool:
        """クリック後に画面が変化したかを検証する。

        生画像のハッシュ比較で「無変化の空振り」を検出する。
        変化がなければ False を返して RECOVERING へ回す。
        """
        try:
            await asyncio.sleep(0.3)
            before = self._last_before_raw
            capture_raw = getattr(self.display, "capture_raw", None)
            after = capture_raw() if callable(capture_raw) else await self._capture_screenshot()
            if before is None:
                # 比較元がなければ成功扱い（従来動作）
                return True
            import hashlib as _hashlib

            h_before = _hashlib.sha256(before.image_bytes).hexdigest()
            h_after = _hashlib.sha256(after.image_bytes).hexdigest()
            if h_before == h_after:
                logger.warning("クリック後に画面変化なし: %s", record.action.params)
                return False
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
            if self._pending_error is not None:
                # 検証失敗などのエラー情報を引き継いで回復計画に使う
                last_error = self._pending_error
            else:
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

    async def _capture_screenshot(self) -> Screenshot:
        """現在のVM画面をキャプチャする（オーバーレイ付き）。

        DPMS 等で画面が真っ黒の場合は Shift キーで起こしてから
        取り直す（最大1回）。エージェントの自己回復。
        """
        if not self.display.is_connected:
            raise RuntimeError("ディスプレイが接続されていません")
        screenshot = self.display.capture_screen()
        if _is_black_screen(screenshot):
            logger.info("画面が真っ黒のためウェイクアップして再取得します")
            try:
                self.display.key_press("shift")
            except Exception:
                logger.debug("ウェイクアップキー送信に失敗", exc_info=True)
            await asyncio.sleep(1.0)
            screenshot = self.display.capture_screen()
        return screenshot

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
