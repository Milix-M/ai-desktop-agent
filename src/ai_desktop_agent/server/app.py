"""FastAPI アプリケーション — AI Desktop Agent のバックエンドサーバー。

WebSocket でフロントエンドと通信し、TaskSession を管理する。
フロントエンドは Next.js で別途配信される。
"""

import contextlib
import logging
from collections.abc import Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ai_desktop_agent.server.session import TaskSession

logger = logging.getLogger(__name__)

app = FastAPI(title="AI Desktop Agent", version="0.1.0")

# CORS: Next.js (port 3000) からの API 呼び出しを許可
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# アクティブなセッション（シングルトン運用）
_active_session: TaskSession | None = None

# テスト用のセッションファクトリ。テストから差し替え可能。
_create_session = TaskSession  # type: ignore[var-annotated]

# WebSocket コールバックのグローバルレジストリ
# セッションより長生きする WebSocket 接続のコールバックを保持し、
# 新セッション作成時に引き継ぐ。
_ws_state_cbs: list[Callable] = []
_ws_action_cbs: list[Callable] = []
_ws_error_cbs: list[Callable] = []
_ws_complete_cbs: list[Callable] = []


class CreateTaskRequest(BaseModel):
    instruction: str


class TaskStatus(BaseModel):
    session_id: str | None
    state: str
    is_running: bool
    action_count: int
    success_count: int
    failure_count: int


# ── REST API ──────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tasks", response_model=TaskStatus)
async def create_task(req: CreateTaskRequest) -> TaskStatus:
    """新しいタスクを作成し、バックグラウンドで実行開始する。"""
    global _active_session

    if _active_session and _active_session.is_running:
        _active_session.stop()

    session = _create_session()

    # 全 WebSocket 接続のコールバックを新セッションに登録
    for cb in _ws_state_cbs:
        session.on_state_change(cb)
    for cb in _ws_action_cbs:
        session.on_action(cb)
    for cb in _ws_error_cbs:
        session.on_error(cb)
    for cb in _ws_complete_cbs:
        session.on_complete(cb)

    _active_session = session

    await session.start_async(req.instruction)
    return _make_status(session)


@app.get("/tasks/current", response_model=TaskStatus)
async def get_current_task() -> TaskStatus:
    """現在のタスク状態を返す。"""
    if _active_session is None:
        return TaskStatus(
            session_id=None,
            state="idle",
            is_running=False,
            action_count=0,
            success_count=0,
            failure_count=0,
        )
    return _make_status(_active_session)


@app.post("/tasks/current/pause")
async def pause_task() -> dict[str, str]:
    if _active_session:
        _active_session.pause()
        return {"status": "paused"}
    return {"status": "no_session"}


@app.post("/tasks/current/resume")
async def resume_task() -> dict[str, str]:
    if _active_session:
        _active_session.resume()
        return {"status": "resumed"}
    return {"status": "no_session"}


@app.post("/tasks/current/stop")
async def stop_task() -> dict[str, str]:
    if _active_session:
        _active_session.stop()
        return {"status": "stopped"}
    return {"status": "no_session"}


# ── WebSocket ─────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """WebSocket エンドポイント。

    フロントエンドが接続し、リアルタイムでエージェントの状態を受け取る。
    コールバックはグローバルレジストリに登録し、セッション生死をまたいで存続する。
    """
    global _active_session
    await ws.accept()
    logger.info("WebSocket 接続")

    if _active_session is None:
        await ws.send_json({"type": "status", "state": "no_session"})

    # セッションのイベントを WebSocket に転送する
    async def on_state(state, ctx):
        with contextlib.suppress(Exception):
            await ws.send_json(
                {
                    "type": "state",
                    "state": state.value,
                    "subtask_index": ctx.current_subtask_index,
                    "subtask_count": len(ctx.subtasks),
                    "action_count": len(ctx.action_history),
                }
            )

    async def on_action(action, success):
        with contextlib.suppress(Exception):
            await ws.send_json(
                {
                    "type": "action",
                    "action_type": action.action_type.value,
                    "description": action.description,
                    "success": success,
                }
            )

    async def on_error(error):
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "error", "message": error})

    async def on_complete(success):
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "complete", "success": success})

    # グローバルレジストリに登録（常に）
    _ws_state_cbs.append(on_state)
    _ws_action_cbs.append(on_action)
    _ws_error_cbs.append(on_error)
    _ws_complete_cbs.append(on_complete)

    # 現在アクティブなセッションにも登録
    if _active_session:
        _active_session.on_state_change(on_state)
        _active_session.on_action(on_action)
        _active_session.on_error(on_error)
        _active_session.on_complete(on_complete)

    try:
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        logger.info("WebSocket 切断")
    except Exception:
        pass
    finally:
        # 切断時にグローバルレジストリから除去
        with contextlib.suppress(ValueError):
            _ws_state_cbs.remove(on_state)
        with contextlib.suppress(ValueError):
            _ws_action_cbs.remove(on_action)
        with contextlib.suppress(ValueError):
            _ws_error_cbs.remove(on_error)
        with contextlib.suppress(ValueError):
            _ws_complete_cbs.remove(on_complete)


# ── ヘルパー ──────────────────────────────────────────


def _make_status(session: TaskSession) -> TaskStatus:
    ctx = session.loop.context
    return TaskStatus(
        session_id=session.id,
        state=session.loop.state.value,
        is_running=session.is_running,
        action_count=len(ctx.action_history),
        success_count=ctx.success_count,
        failure_count=ctx.failure_count,
    )
