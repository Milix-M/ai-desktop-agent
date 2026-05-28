"""FastAPI アプリケーション — AI Desktop Agent のバックエンドサーバー。

WebSocket でフロントエンドと通信し、TaskSession を管理する。
"""

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ai_desktop_agent.server.session import TaskSession

logger = logging.getLogger(__name__)

app = FastAPI(title="AI Desktop Agent", version="0.1.0")

# アクティブなセッション（シングルトン運用）
_active_session: TaskSession | None = None


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
        # 既存タスクを停止
        _active_session.stop()

    session = TaskSession()
    _active_session = session

    # WebSocket 用のブリッジ: 状態変化をキューに蓄積
    # （create_task ではまだ WebSocket は接続されていないので、
    #   イベントは WS 接続後に送信される）

    await session.start_async(req.instruction)
    return _make_status(session)


@app.get("/tasks/current", response_model=TaskStatus)
async def get_current_task() -> TaskStatus:
    """現在のタスク状態を返す。"""
    if _active_session is None:
        return TaskStatus(
            session_id=None, state="idle", is_running=False,
            action_count=0, success_count=0, failure_count=0,
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
    """
    global _active_session
    await ws.accept()
    logger.info("WebSocket 接続")

    if _active_session is None:
        await ws.send_json({"type": "status", "state": "no_session"})

    # セッションのイベントを WebSocket に転送する
    async def on_state(state, ctx):
        try:
            await ws.send_json({
                "type": "state",
                "state": state.value,
                "subtask_index": ctx.current_subtask_index,
                "subtask_count": len(ctx.subtasks),
                "action_count": len(ctx.action_history),
            })
        except Exception:
            pass

    async def on_action(action, success):
        try:
            await ws.send_json({
                "type": "action",
                "action_type": action.action_type.value,
                "description": action.description,
                "success": success,
            })
        except Exception:
            pass

    async def on_error(error):
        try:
            await ws.send_json({"type": "error", "message": error})
        except Exception:
            pass

    async def on_complete(success):
        try:
            await ws.send_json({"type": "complete", "success": success})
        except Exception:
            pass

    if _active_session:
        _active_session.on_state_change(on_state)
        _active_session.on_action(on_action)
        _active_session.on_error(on_error)
        _active_session.on_complete(on_complete)

    try:
        while True:
            # クライアントからのメッセージを待つ（ping用）
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        logger.info("WebSocket 切断")
    except Exception:
        pass


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
