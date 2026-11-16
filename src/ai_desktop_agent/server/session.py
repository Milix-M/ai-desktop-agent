"""タスクセッション — エージェントループの実行を管理する。

AgentLoop + ActionExecutor + LLMProvider を束ね、
1つのタスクを最初から最後まで実行する。

画面には常に座標グリッド＋カーソル位置が重畳される（VNCClientが自動付与）。
region_select による2段階精密クリックをサポートする。
"""

import asyncio
import contextlib
import inspect
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import replace

from ai_desktop_agent.actions.executor import ActionExecutor
from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.decision.provider import (
    DecisionProvider,
    create_decision_provider_from_env,
)
from ai_desktop_agent.agent.llm.base import LLMProvider
from ai_desktop_agent.agent.llm.factory import create_llm_provider
from ai_desktop_agent.agent.llm.types import (
    ActionDecision,
    ErrorContext,
    RecoveryPlan,
    RecoveryStrategy,
    VerificationResult,
)
from ai_desktop_agent.agent.loop import AgentLoop
from ai_desktop_agent.agent.state import AgentState, Goal, Subtask
from ai_desktop_agent.server.store import StoredAction, TaskRecord, TaskStore
from ai_desktop_agent.vm.async_io import run_display_call, run_vm_call
from ai_desktop_agent.vm.base import DisplayBackend
from ai_desktop_agent.vm.screenshot import Screenshot
from ai_desktop_agent.vm.vnc_client import VNCClient

logger = logging.getLogger(__name__)

#: 真っ黒判定の輝度しきい値（DPMS 消灯時は全画素 0）
BLACK_SCREEN_THRESHOLD = 2.0


def _image_hash(image_bytes: bytes) -> str:
    """画像のSHA256ハッシュを返す（変化検出用）。"""
    import hashlib

    return hashlib.sha256(image_bytes).hexdigest()


def _params_key(params: dict | None) -> tuple:
    """アクション比較用の正規化キー。"""
    return tuple(sorted((params or {}).items()))


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
MAX_EMPTY_RETRIES = 5  # 空応答の決定再試行上限（一時不調の吸収用）
EMPTY_RETRY_SHORT_ATTEMPTS = 2  # 前ターンも失敗時の試行上限（障害時の空費防止用）


RESTART_SETTLE_SECONDS = 60.0  # VM再起動後の起動待ち秒数
STUCK_REPEAT_COUNT = 3  # 同一アクション連続とみなす回数
STUCK_WAIT_SECONDS = 2.0  # 足踏み検出時の待機秒数
SETTLE_TIMEOUT = 5.0  # 画面安定待ちの上限秒数
#: Android-x86 スリープ復帰の長押し秒数（公式docは1秒以上を要求）
ANDROID_WAKE_HOLD_SECONDS = 1.1

# 画面を変える可能性のあるアクション（実行後に安定待ちする）
VISUAL_ACTIONS: frozenset = frozenset(
    {
        ActionType.LEFT_CLICK,
        ActionType.RIGHT_CLICK,
        ActionType.MIDDLE_CLICK,
        ActionType.DOUBLE_CLICK,
        ActionType.DRAG,
        ActionType.SCROLL,
        ActionType.TYPE,
        ActionType.KEY_PRESS,
        ActionType.KEY_COMBO,
        ActionType.KEY_HOLD,
    }
)


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
        vm_id: str | None = None,
        decision: DecisionProvider | None | str = "auto",
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.loop = AgentLoop()
        self.llm = llm or create_llm_provider()
        # 決定モデル（完了確認・回復方針のみ。次操作決めは対象外）。
        # "auto"は環境変数DECISION_PROVIDERに従い、未設定ならNone（従来動作）。
        if decision == "auto":
            try:
                decision = create_decision_provider_from_env()
            except ValueError:
                logger.exception("決定モデルの初期化に失敗したため無効化します")
                decision = None
        self.decision = decision
        # 決定モデルの利用実績（コスト試算用）
        self.decision_calls = 0
        self.decision_escalations = 0
        if hasattr(self.llm, "session_id"):
            # 会話単位IDをLLMに引き渡す（OpenCode Go の x-opencode-session 用）。
            # タスク内では不変のため、生成直後の1回だけ設定する。
            self.llm.session_id = self.id
        self._store = store
        self._instruction = ""
        self._created_at = time.time()
        # 実行対象VMのID（複数VMの並列実行用）
        self.vm_id = vm_id
        # ユーザー許可制：エージェント判断でのVM再起動を許可するか
        self.allow_vm_restart = False

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
        self._cleanup_task: asyncio.Task | None = None
        self._resources_closed = False
        # 検証用：実行直前の生画像（オーバーレイなし）
        self._last_before_raw: Screenshot | None = None
        # サブタスクごとの空完了却下回数
        self._empty_complete_refusals: dict[str, int] = {}
        # サブタスクごとの検証失敗回数
        self._verify_failures: dict[str, int] = {}
        # 回復・再決定に引き継ぐエラー情報
        self._pending_error: ErrorContext | None = None
        # 前ターンの画面ハッシュ（無変化検出用）
        self._prev_turn_hash: str | None = None
        # 連続する決定取得の失敗回数（空応答ストーム時の早期打切り用）
        self._consec_empty_decides: int = 0
        self._replan_count = 0

        # イベントコールバック
        self._on_state_change: list[Callable] = []
        self._on_action: list[Callable] = []
        self._on_error: list[Callable] = []
        self._on_complete: list[Callable] = []
        self._control_notifications: set[asyncio.Task] = set()

    def set_display(self, display: DisplayBackend) -> None:
        """操作対象の表示バックエンドを差し替える（VM指定用）。"""
        try:
            old = self.display
            if old is not None and old is not display:
                with contextlib.suppress(Exception):
                    old.disconnect()
        except AttributeError:
            pass
        self.display = display
        self.executor = ActionExecutor(self.display)

    # ── イベント ──────────────────────────────

    def on_state_change(self, cb: Callable) -> None:
        self._on_state_change.append(cb)

    def on_action(self, cb: Callable) -> None:
        self._on_action.append(cb)

    def on_error(self, cb: Callable) -> None:
        self._on_error.append(cb)

    def on_complete(self, cb: Callable) -> None:
        self._on_complete.append(cb)

    async def _notify(self, callbacks: list[Callable], *args) -> None:
        for callback in tuple(callbacks):
            try:
                result = callback(*args)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("イベント通知に失敗")

    async def _emit_state_change(self) -> None:
        self._persist()
        await self._notify(self._on_state_change, self.loop.state, self.loop.context)

    async def _emit_action(self, action: Action, success: bool) -> None:
        self._persist()
        await self._notify(self._on_action, action, success)

    async def _emit_error(self, error: str) -> None:
        await self._notify(self._on_error, error)

    async def _emit_complete(self, success: bool) -> None:
        await self._notify(self._on_complete, success)

    # ── メインループ ───────────────────────────

    async def run(self, instruction: str) -> bool:
        """タスクを最初から最後まで実行する。

        Returns:
            タスクが正常に完了したかどうか。
        """
        goal = Goal(description=instruction)
        self._instruction = instruction
        self.loop.start(goal)
        try:
            await self._emit_state_change()
            # Phase 1: UNDERSTANDING
            understanding = await self.llm.understand_instruction(goal, platform=self._platform())
            goal.intent = understanding.intent
            goal.target_application = understanding.target_application
            goal.constraints = understanding.constraints
            self.loop.understanding_done()
            await self._emit_state_change()

            # Phase 2: PLANNING
            decomposition = await self.llm.decompose_task(goal, 0, platform=self._platform())
            if not decomposition.subtasks:
                self.loop.planning_failed()
                await self._emit_error("タスクの分解に失敗しました")
                await self._emit_state_change()
                await self._emit_complete(False)
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
            self._persist(success=success)
            await self._emit_complete(success)
            return success

        except asyncio.CancelledError:
            # stop() による中断：状態を FAILED に倒して保存（沈黙終了を防ぐ）
            import contextlib

            logger.warning("タスクがキャンセルされました")
            with contextlib.suppress(ValueError):
                self.loop.fail_now()
            await self._emit_state_change()
            self._persist(success=False)
            await self._emit_complete(False)
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
            await self._emit_complete(False)
            return False

        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Release the display and provider connections on every exit path."""
        if self._resources_closed:
            return
        self._resources_closed = True
        try:
            await asyncio.to_thread(self.display.disconnect)
        except Exception:
            logger.exception("VNC接続の解放に失敗")
        for provider in (self.llm, self.decision):
            close = getattr(provider, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    logger.exception("プロバイダ接続の解放に失敗")

    def _ensure_closed(self, task: asyncio.Task) -> None:
        # Cancellation before run() starts never enters its finally block.
        if not self._resources_closed:
            self._cleanup_task = asyncio.create_task(self.aclose())

    # ── 永続化 ────────────────────────────────────

    def snapshot(self, success: bool | None = None) -> TaskRecord:
        """現在のコンテキストを永続化用レコードに変換する。"""
        ctx = self.loop.context
        goal = ctx.goal
        if success is None and self.loop.is_done:
            success = self.loop.state == AgentState.COMPLETED
        return TaskRecord(
            id=self.id,
            instruction=self._instruction,
            state=self.loop.state.value,
            success=success,
            prompt_tokens=int(getattr(self.llm, "total_prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(self.llm, "total_completion_tokens", 0) or 0),
            llm_calls=int(getattr(self.llm, "call_count", 0) or 0),
            decision_calls=int(getattr(self, "decision_calls", 0) or 0),
            decision_escalations=int(getattr(self, "decision_escalations", 0) or 0),
            actions=[
                StoredAction(
                    action_type=r.action.action_type.value,
                    params=dict(r.action.params or {}),
                    description=r.action.description,
                    success=r.success,
                    error_message=r.error_message,
                    duration_ms=r.duration_ms,
                    at=r.at,
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
            current_subtask_index=ctx.current_subtask_index,
            vm_id=self.vm_id,
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
            revision=self.loop.transition_count,
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
        # 前ターンから画面が無変化なら、その旨も伝える（低速VM対策）。
        error_ctx = self._pending_error
        self._pending_error = None
        if error_ctx is None and self._is_screen_unchanged(screenshot):
            error_ctx = ErrorContext(
                action=self.loop.context.action_history[-1].action,
                error_message=(
                    "前回の操作後も画面に変化がありません。"
                    "アプリの起動には時間がかかる場合があります。"
                    "同じ操作の連打は避け、wait で待機するか、別の手段を試してください。"
                ),
                retry_count=0,
            )
        decision = await self._decide_action(subtask, screenshot, error_ctx)
        self._prev_turn_hash = _image_hash(screenshot.image_bytes)
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

        # VM再起動は専用フローで処理する
        await self._wait_if_paused()
        if decision.action.action_type == ActionType.VM_RESTART:
            await self._execute_vm_restart(decision)
            return
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
            await self._wait_if_paused()
            if decision is None:
                self.loop.action_executed()
                self.loop.wait_complete()
                self.loop.verify_failed()
                await self._emit_state_change()
                return
        # region_select の処理：ズーム → 再判断 → 精密操作
        elif decision.action.action_type == ActionType.REGION_SELECT:
            decision = await self._handle_region_zoom(subtask, decision)
            await self._wait_if_paused()
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

        # 同一アクションの足踏みは待機に置換する（低速VM対策）
        if self._is_stuck_repeat(decision.action):
            logger.info("同一操作の連続を検出、待機に置換します: %s", decision.action.params)
            decision = ActionDecision(
                action=Action(action_type=ActionType.WAIT, params={"seconds": STUCK_WAIT_SECONDS}),
                expected_effect="画面変化の待機",
                confidence=decision.confidence,
                reasoning="同一操作の連続検出による待機: " + decision.reasoning,
            )

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
            self._last_before_raw = (
                await run_display_call(capture_raw) if callable(capture_raw) else None
            )
        except Exception:
            self._last_before_raw = None
        started = time.monotonic()
        success = await self.executor.execute(action)
        self.loop.record_action(
            action,
            success,
            error=self.executor.last_error,
            reasoning=decision.reasoning,
            confidence=decision.confidence,
            duration_ms=(time.monotonic() - started) * 1000,
        )

        # 画面変化を待つ。視覚系アクション後は安定するまで待機し、
        # 低速VMでの早すぎる再観測（＝同じ操作の繰返し原因）を防ぐ。
        if action.action_type in VISUAL_ACTIONS:
            await self.executor.wait_for_still(SETTLE_TIMEOUT)
        elif action.action_type in (ActionType.WAIT, ActionType.MOUSE_MOVE):
            # 待機自体が待機・移動は画面を変えないため追加待機なし
            pass
        else:
            await asyncio.sleep(0.5)

        # 一時停止中は再開まで待つ（遷移エラーを防ぐ）
        await self._wait_if_paused()

        # 状態遷移。SUBTASK_COMPLETEもVERIFYINGで止める。
        # 達成の画像検証は_verify_phase（_verify_subtask_phase）に委ねる。
        # ここでverify_subtask_done()まで進めると検証が素通りになる。
        self.loop.action_executed()
        self.loop.wait_complete()

        await self._emit_action(action, success)
        await self._emit_state_change()

    async def _wait_if_paused(self) -> None:
        """PAUSED の間は再開まで待機する。"""
        while self.loop.state == AgentState.PAUSED:
            await asyncio.sleep(0.5)

    async def _execute_vm_restart(self, decision: ActionDecision) -> None:
        """VM_RESTART を処理する：許可制で再起動、後は回復フローへ。

        許可なし → 却下して回復フロー（別手段へ）。
        実行後 → 環境初期化として回復フローへ（新画面で再判断）。
        """
        from ai_desktop_agent.server.vm_pool import VmPool

        await self._wait_if_paused()
        action = decision.action
        reason = (action.params or {}).get("reason", "")

        if not self.allow_vm_restart:
            logger.info("VM再起動は許可されていないため却下: %s", reason)
            self.loop.record_action(action, False, "VM再起動は許可されていません")
            self._pending_error = ErrorContext(
                action=action,
                error_message=(
                    "VM再起動はこのタスクでは許可されていません。"
                    "VMを作り直さずに別の方法で進めてください。"
                ),
                retry_count=0,
            )
            self.loop.action_executed()
            self.loop.wait_complete()
            self.loop.verify_failed()
            await self._emit_action(action, False)
            await self._emit_state_change()
            return

        try:
            pool = VmPool()
            info = await run_vm_call(pool.restart_vm, self.vm_id or "vm")
            logger.info("VM再起動を実行: %s (%s)", info.id, reason)
            await asyncio.sleep(RESTART_SETTLE_SECONDS)
            ok, error = True, ""
        except Exception as e:
            logger.exception("VM再起動に失敗")
            ok, error = False, f"VM再起動に失敗しました: {e}"

        await self._wait_if_paused()
        self.loop.record_action(action, ok, error)
        if ok:
            self._pending_error = ErrorContext(
                action=action,
                error_message=(
                    "VMを再起動しました。ディスクの初期化は行っていません。"
                    "画面を確認し、保存済みの作業をやり直さずに続きから計画してください。"
                ),
                retry_count=0,
            )
        else:
            self._pending_error = ErrorContext(
                action=action,
                error_message=error,
                retry_count=0,
            )
        self.loop.action_executed()
        self.loop.wait_complete()
        self.loop.verify_failed()
        await self._emit_action(action, ok)
        await self._emit_state_change()

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
            raw = await run_display_call(self.display.capture_raw)
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
                platform=self._platform(),
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

    def _is_screen_unchanged(self, screenshot: Screenshot) -> bool:
        """前ターンの画面と同一かどうか（低速VMの無変化検出用）。"""
        if self._prev_turn_hash is None or not self.loop.context.action_history:
            return False
        return _image_hash(screenshot.image_bytes) == self._prev_turn_hash

    def _is_stuck_repeat(self, action: Action) -> bool:
        """同一アクションが連続しているか（SUBTASK_COMPLETE除く）。"""
        if action.action_type == ActionType.SUBTASK_COMPLETE:
            return False
        history = self.loop.context.action_history
        if len(history) < STUCK_REPEAT_COUNT - 1:
            return False
        key = (action.action_type, _params_key(action.params))
        recent = history[-(STUCK_REPEAT_COUNT - 1) :]
        return all(
            (r.action.action_type, _params_key(r.action.params or {})) == key for r in recent
        )

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
            result: VerificationResult | None = None
            # 決定モデルを試す（処理順3番目の完了確認）。中間・失敗時はLLMに退避。
            if self.decision is not None:
                try:
                    judged = await self.decision.verify_completion(
                        expected_outcome=subtask.expected_outcome,
                        evidence_text=record.reasoning,
                        confidence_hint=record.confidence,
                        image_bytes=screenshot.image_bytes,
                    )
                    self.decision_calls += 1
                    if judged.success is not None:
                        logger.info(
                            "完了確認を決定モデルで判定: success=%s prob=%.2f grade=%s",
                            judged.success,
                            judged.prob,
                            judged.grade,
                        )
                        result = VerificationResult(
                            success=judged.success,
                            reasoning=judged.reasoning,
                            evidence=f"decision prob={judged.prob} grade={judged.grade}",
                        )
                    else:
                        self.decision_escalations += 1
                        logger.info(
                            "完了確認が中間帯 (prob=%.2f grade=%s) のためLLMに再確認します",
                            judged.prob,
                            judged.grade,
                        )
                except Exception:
                    self.decision_escalations += 1
                    logger.exception("決定モデル判定に失敗、LLMに退避します")
            if result is None:
                result = await self.llm.verify_result(
                    decision,
                    subtask.expected_outcome,
                    screenshot=screenshot,
                    expected_outcome=subtask.expected_outcome,
                    platform=self._platform(),
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
        直前の安定待ちで画面は収束済みのため、短い再観測で足りる。
        """
        try:
            await asyncio.sleep(0.1)
            before = self._last_before_raw
            capture_raw = getattr(self.display, "capture_raw", None)
            after = (
                await run_display_call(capture_raw)
                if callable(capture_raw)
                else await self._capture_screenshot()
            )
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
        last_error = self._pending_error
        if last_error is None and self.loop.context.action_history:
            record = self.loop.context.action_history[-1]
            if not record.success:
                last_error = ErrorContext(record.action, record.error_message, 0)
        if last_error is None:
            self.loop.recover_retry()
            return
        retry_key = f"{subtask.id}:{last_error.action.action_type.value}"
        retries = self.loop.context.retry_counts.get(retry_key, 0)
        if retries >= subtask.max_retries:
            self.loop.recover_failed()
            await self._emit_error(f"再試行の上限に到達しました: {subtask.description}")
            await self._emit_state_change()
            return
        self.loop.context.retry_counts[retry_key] = retries + 1
        last_error.retry_count = retries

        # 決定モデルを試す（処理順4番目の回復方針決定）。確信度不足・失敗時はLLMに退避。
        plan: RecoveryPlan | None = None
        if self.decision is not None:
            try:
                history_text = "\n".join(
                    f"{r.action.action_type.value} {r.action.params} "
                    f"{'OK' if r.success else 'NG:' + (r.error_message or '')}"
                    for r in self.loop.context.action_history[-10:]
                )
                judged = await self.decision.choose_recovery(
                    error_message=last_error.error_message,
                    subtask_description=subtask.description,
                    history_text=history_text,
                    retry_count=last_error.retry_count,
                )
                self.decision_calls += 1
                if not judged.escalate:
                    logger.info(
                        "回復方針を決定モデルで選択: %s prob=%.2f",
                        judged.strategy,
                        judged.prob,
                    )
                    plan = RecoveryPlan(
                        strategy=RecoveryStrategy(judged.strategy),
                        actions=[],
                        reasoning=judged.reasoning,
                        recoverable=judged.recoverable,
                    )
                else:
                    self.decision_escalations += 1
                    logger.info(
                        "回復方針の確信度が不足 (prob=%.2f) のためLLMに退避します",
                        judged.prob,
                    )
            except Exception:
                self.decision_escalations += 1
                logger.exception("回復方針の決定モデル判定に失敗、LLMに退避します")
        if plan is None:
            plan = await self.llm.recover_from_error(
                last_error,
                self.loop.context.action_history,
                subtask,
                platform=self._platform(),
            )

        if not plan.recoverable or plan.strategy == RecoveryStrategy.GIVE_UP:
            self.loop.recover_failed()
            await self._emit_state_change()
            return

        # Preserve the chosen strategy for the next screenshot-based decision.
        self._pending_error = ErrorContext(
            action=last_error.action,
            error_message=f"{last_error.error_message}\n回復方針: {plan.reasoning}",
            retry_count=retries + 1,
        )
        if not await self._execute_recovery_actions(plan, retries):
            return
        if plan.strategy == RecoveryStrategy.REPLAN_SUBTASK:
            if self._replan_count >= 3:
                self.loop.recover_failed()
                await self._emit_error("再計画の上限に到達しました")
                await self._emit_state_change()
                return
            self._replan_count += 1
            self.loop.recover_replan()
            await self._emit_state_change()
            ctx = self.loop.context
            completed = ctx.subtasks[: ctx.current_subtask_index]
            remaining = ctx.subtasks[ctx.current_subtask_index :]
            goal = replace(
                ctx.goal or Goal(description=self._instruction),
                constraints=[
                    *(ctx.goal.constraints if ctx.goal else []),
                    "完了済み（やり直し不要）: " + "; ".join(s.description for s in completed),
                    "残りの作業を再計画する: " + "; ".join(s.description for s in remaining),
                    f"回復方針: {plan.reasoning}",
                ],
            )
            decomposition = await self.llm.decompose_task(
                goal, len(completed), platform=self._platform()
            )
            if not decomposition.subtasks:
                self.loop.planning_failed()
                await self._emit_error("再計画に失敗しました")
            else:
                subtasks = [
                    replace(s, id=f"replan_{self._replan_count}_{i}")
                    for i, s in enumerate(decomposition.subtasks)
                ]
                self.loop.plan_ready([*completed, *subtasks], completed_count=len(completed))
            await self._emit_state_change()
            return

        self.loop.recover_retry()
        await self._emit_state_change()

    async def _execute_recovery_actions(self, plan: RecoveryPlan, retries: int) -> bool:
        actions = plan.actions
        if not actions and plan.strategy == RecoveryStrategy.WAIT_AND_RETRY:
            actions = [Action(ActionType.WAIT, {"seconds": STUCK_WAIT_SECONDS})]
        for action in actions:
            if len(self.loop.context.action_history) >= MAX_ACTIONS_PER_TASK:
                self.loop.recover_failed()
                await self._emit_error("アクション上限に到達しました")
                await self._emit_state_change()
                return False
            # Restart permission and completion verification also apply to recovery actions.
            if action.action_type == ActionType.VM_RESTART:
                self.loop.recover_retry()
                await self._execute_vm_restart(ActionDecision(action, "", 1.0, plan.reasoning))
                return False
            if action.action_type in (ActionType.REGION_SELECT, ActionType.SUBTASK_COMPLETE):
                break
            started = time.monotonic()
            success = await self.executor.execute(action)
            self.loop.record_action(
                action,
                success,
                error=self.executor.last_error,
                duration_ms=(time.monotonic() - started) * 1000,
                reasoning=plan.reasoning,
            )
            await self._emit_action(action, success)
            if not success:
                self._pending_error = ErrorContext(action, self.executor.last_error, retries + 1)
                await self._emit_state_change()
                return False
        return True

    # ── アクション決定 ─────────────────────────

    async def _decide_action(
        self,
        subtask: Subtask,
        screenshot: Screenshot,
        error_context: ErrorContext | None = None,
    ) -> ActionDecision:
        """LLM に次のアクションを決定させる（画面キャプチャ付き）。

        空応答・JSON破損は通信路の一時不調として上限まで再試行する
        （履歴を汚さないよう決定取得の内側で吸収する）。
        前ターンも失敗している場合は試行を絞って早期に打ち切る
        （障害時の長時間空費を防ぐ。結論のFAILEDは変わらない）。
        アクション検証エラー（必須パラメータ不足など）は決定的なため即時送出する。
        """
        max_attempts = (
            EMPTY_RETRY_SHORT_ATTEMPTS if self._consec_empty_decides > 0 else MAX_EMPTY_RETRIES + 1
        )
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                decision = await self.llm.decide_next_action(
                    goal=self.loop.context.goal or Goal(description=""),
                    current_subtask=subtask,
                    action_history=self.loop.context.action_history,
                    screenshot=screenshot,
                    error_context=error_context,
                    platform=self._platform(),
                )
                self._consec_empty_decides = 0
                return decision
            except ValueError as e:
                retryable = isinstance(e, json.JSONDecodeError) or "応答が空" in str(e)
                if not retryable or attempt >= max_attempts - 1:
                    self._consec_empty_decides += 1
                    raise
                last_error = e
                logger.warning(
                    "決定応答の不備のため再試行します (%d/%d): %s",
                    attempt + 1,
                    max_attempts,
                    str(e)[:80],
                )
                await asyncio.sleep(2.0 * (attempt + 1))
        raise last_error or ValueError("応答が空です")

    async def _capture_screenshot(self) -> Screenshot:
        """現在のVM画面をキャプチャする（オーバーレイ付き）。

        DPMS 等で画面が真っ黒の場合は起こしてから取り直す（最大1回）。
        Linux系は Shift で復帰するが、Android-x86 は Shift では復帰せず
        ESC / Menu の1秒超ホールドが必要（公式doc準拠）のため、
        Android端末（vm_id が ``android`` / ``and-*``）では専用シーケンスを使う。
        """
        if not self.display.is_connected:
            raise RuntimeError("ディスプレイが接続されていません")
        screenshot = await run_display_call(self.display.capture_screen)
        if _is_black_screen(screenshot):
            logger.info("画面が真っ黒のためウェイクアップして再取得します")
            try:
                if self._is_android_vm():
                    await self._wake_android_display()
                else:
                    await run_display_call(self.display.key_press, "shift")
            except Exception:
                logger.debug("ウェイクアップキー送信に失敗", exc_info=True)
            await asyncio.sleep(1.0)
            screenshot = await run_display_call(self.display.capture_screen)
        return screenshot

    def _is_android_vm(self) -> bool:
        """操作対象がAndroid端末かどうか（ウェイク方式の切替用）。"""
        vm_id = self.vm_id or ""
        return vm_id == "android" or vm_id.startswith("and-")

    def _platform(self) -> str:
        """LLMプロンプト切替用のプラットフォーム名（android/linux）。"""
        from ai_desktop_agent.agent.llm.openai_compat_provider import (
            PLATFORM_ANDROID,
            PLATFORM_LINUX,
        )

        return PLATFORM_ANDROID if self._is_android_vm() else PLATFORM_LINUX

    async def _wake_android_display(self) -> None:
        """Android-x86用スリープ復帰シーケンスを送る。

        DisplayBackend が ``wake_android()`` を持てばそれを使い、
        なければ ESC / Menu の長押し＋中央タップで代用する。
        """
        wake = getattr(self.display, "wake_android", None)
        if callable(wake):
            try:
                await run_display_call(wake)
                return
            except Exception:
                logger.debug("wake_android() に失敗、フォールバックする", exc_info=True)
        # フォールバック（wake_android 未実装バックエンド用）
        try:
            await run_display_call(self.display.key_down, "escape")
            await asyncio.sleep(ANDROID_WAKE_HOLD_SECONDS)
            await run_display_call(self.display.key_up, "escape")
            await asyncio.sleep(0.3)
            await run_display_call(self.display.key_down, "menu")
            await asyncio.sleep(ANDROID_WAKE_HOLD_SECONDS)
            await run_display_call(self.display.key_up, "menu")
            await asyncio.sleep(0.3)
        except Exception:
            logger.debug("Androidウェイクキー送信に失敗", exc_info=True)
            return
        try:
            width = (
                getattr(self.display, "_screen_width", None)
                or getattr(self.display, "_width", None)
                or 1024
            )
            height = (
                getattr(self.display, "_screen_height", None)
                or getattr(self.display, "_height", None)
                or 768
            )
            await run_display_call(self.display.mouse_click, int(width // 2), int(height // 2))
        except Exception:
            logger.debug("Androidウェイクタップに失敗", exc_info=True)

    # ── 制御 ────────────────────────────────────

    async def start_async(self, instruction: str) -> None:
        """バックグラウンドでタスクを開始する。"""
        if self._task and not self._task.done():
            raise RuntimeError("タスクは既に実行中です")
        self._task = asyncio.create_task(self.run(instruction))
        self._task.add_done_callback(self._ensure_closed)

    def pause(self) -> None:
        if self.loop.state == AgentState.EXECUTING:
            self.loop.pause()
            self._notify_control_change()

    def resume(self) -> None:
        if self.loop.state == AgentState.PAUSED:
            self.loop.resume()
            self._notify_control_change()

    def _notify_control_change(self) -> None:
        # 制御は同期APIを保ち、待機中の実行ループに依存せず通知する。
        self._persist()
        task = asyncio.create_task(self._emit_state_change())
        self._control_notifications.add(task)
        task.add_done_callback(self._control_notification_done)

    def _control_notification_done(self, task: asyncio.Task) -> None:
        self._control_notifications.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("タスク制御の状態通知に失敗", exc_info=task.exception())

    def stop(self) -> None:
        if self._task and not self._task.done():
            request_stop = getattr(self.display, "request_stop", None)
            if callable(request_stop):
                request_stop()
            self._task.cancel()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()
