"""FastAPI アプリケーション — AI Desktop Agent のバックエンドサーバー。

WebSocket でフロントエンドと通信し、TaskSession を管理する。
フロントエンドは Next.js で別途配信される。
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.server.vm_control import (
    DockerUnavailableError,
    get_vm_controller,
)
from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.vm.vnc_client import VNCClient

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)


# ── 永続化ストア（遅延初期化：テスト時は差し替え可能） ──

_store: TaskStore | None = None


def get_store() -> TaskStore:
    """タスク永続化ストアを返す（初回利用時に初期化）。"""
    global _store
    if _store is None:
        _store = TaskStore()
    return _store


def _default_create_session() -> TaskSession:
    return TaskSession(store=get_store())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """起動時に中断タスクをマークする。"""
    try:
        marked = get_store().mark_interrupted()
        if marked:
            logger.info("起動時に中断タスク %d 件をマーク", marked)
    except Exception:
        logger.exception("タスクストアの初期化に失敗")
    yield


app = FastAPI(title="AI Desktop Agent", version="0.1.0", lifespan=lifespan)

# CORS: Next.js (port 3000) からの API 呼び出しを許可
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# アクティブなセッション（後方互換：最新の1件）
_active_session: TaskSession | None = None

# 全セッション（複数VMの並列実行用）： session_id -> TaskSession
_sessions: dict[str, TaskSession] = {}

# テスト用のセッションファクトリ。テストから差し替え可能。
_create_session = _default_create_session  # type: ignore[var-annotated]

# VMプール（遅延初期化：テスト時は差し替え可能）
_pool: VmPool | None = None


def get_pool() -> VmPool:
    """VMプールを返す（初回利用時に初期化）。"""
    global _pool
    if _pool is None:
        _pool = VmPool()
    return _pool


# WebSocket コールバックのグローバルレジストリ
# セッションより長生きする WebSocket 接続のコールバックを保持し、
# 新セッション作成時に引き継ぐ。
_ws_state_cbs: list[Callable] = []
_ws_action_cbs: list[Callable] = []
_ws_error_cbs: list[Callable] = []
_ws_complete_cbs: list[Callable] = []


class CreateTaskRequest(BaseModel):
    instruction: str
    vm_id: str | None = None  # 省略時は稼働中の既定VM


class TaskStatus(BaseModel):
    session_id: str | None
    state: str
    is_running: bool
    action_count: int
    success_count: int
    failure_count: int
    subtasks: list[dict] = []
    current_subtask_index: int = 0
    vm_id: str | None = None


class VmStatus(BaseModel):
    running: bool
    status: str
    health: str | None = None
    name: str | None = None


class VmInfoModel(BaseModel):
    id: str
    name: str
    status: str
    health: str | None = None
    vnc_port: int = 5900
    ws_port: int = 6080
    vnc_host: str = ""
    managed: bool = True


class CreateVmRequest(BaseModel):
    name: str | None = None


class StoredActionItem(BaseModel):
    action_type: str
    params: dict = {}
    description: str = ""
    success: bool = True
    error_message: str = ""
    duration_ms: float = 0.0
    at: float = 0.0
    reasoning: str = ""
    confidence: float = 1.0


class TaskSummary(BaseModel):
    id: str
    instruction: str
    state: str
    success: bool | None = None
    action_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    updated_at: float = 0.0
    vm_id: str | None = None


class TaskDetail(TaskSummary):
    actions: list[StoredActionItem] = []
    subtasks: list[dict] = []
    current_subtask_index: int = 0
    goal: dict = {}
    created_at: float = 0.0


def _to_summary(d: dict) -> TaskSummary:
    return TaskSummary(
        id=d.get("id", ""),
        instruction=d.get("instruction", ""),
        state=d.get("state", "idle"),
        success=d.get("success"),
        action_count=d.get("action_count", 0),
        success_count=d.get("success_count", 0),
        failure_count=d.get("failure_count", 0),
        updated_at=d.get("updated_at", 0.0),
        vm_id=d.get("vm_id"),
    )


# テスト用の VM コントローラファクトリ。テストから差し替え可能。
_get_vm_controller = get_vm_controller


# ── REST API ──────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tasks", response_model=TaskStatus)
async def create_task(req: CreateTaskRequest) -> TaskStatus:
    """新しいタスクを作成し、バックグラウンドで実行開始する。

    vm_id 省略時は稼働中の既定VMを使う。複数VMで並列実行できる。
    """
    global _active_session

    vm = _resolve_vm(req.vm_id)
    display = _connect_vm_display(vm)

    if _active_session and _active_session.is_running and _active_session.vm_id == vm.id:
        _active_session.stop()

    session = _create_session()
    session.vm_id = vm.id
    session.set_display(display)

    # 全 WebSocket 接続にセッション情報を付与して配送する
    _forward_registrations(session)

    _active_session = session
    _sessions[session.id] = session

    await session.start_async(req.instruction)
    return _make_status(session)


def _resolve_vm(vm_id: str | None):
    """タスク投入先のVMを解決する。"""
    from fastapi import HTTPException

    pool = get_pool()
    if vm_id:
        vm = pool.get_vm(vm_id)
        if vm is None:
            raise HTTPException(status_code=404, detail=f"vm not found: {vm_id}")
        if vm.status != "running":
            raise HTTPException(status_code=409, detail=f"vm not running: {vm_id}")
        return vm
    vm = pool.default_vm()
    if vm is None:
        raise HTTPException(status_code=409, detail="利用可能なVMがありません")
    return vm


def _connect_vm_display(vm):
    """VMへのVNC接続を作る。"""
    from fastapi import HTTPException

    try:
        display = VNCClient()
        display.connect(vm.vnc_host or "vm", 5900)
        return display
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"VMへのVNC接続に失敗: {e}") from e


@app.get("/vms", response_model=list[VmInfoModel])
async def list_vms() -> list[VmInfoModel]:
    """VM一覧を返す。"""
    from fastapi import HTTPException

    try:
        return [VmInfoModel(**v.to_dict()) for v in get_pool().list_vms()]
    except DockerUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.post("/vms", response_model=VmInfoModel)
async def create_vm(req: CreateVmRequest) -> VmInfoModel:
    """新しいVMを作成して起動する。"""
    from fastapi import HTTPException

    try:
        info = get_pool().create_vm(name=req.name)
    except DockerUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return VmInfoModel(**info.to_dict())


@app.delete("/vms/{vm_id}")
async def delete_vm(vm_id: str) -> dict[str, str]:
    """VMを削除する（上のタスクは停止する）。"""
    from fastapi import HTTPException

    global _active_session

    for sid, sess in list(_sessions.items()):
        if getattr(sess, "vm_id", None) == vm_id:
            sess.stop()
            del _sessions[sid]
    if _active_session and getattr(_active_session, "vm_id", None) == vm_id:
        _active_session = None

    try:
        ok = get_pool().remove_vm(vm_id)
    except DockerUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=404, detail="vm not found")
    return {"status": "deleted"}


@app.get("/tasks/current", response_model=TaskStatus)
async def get_current_task() -> TaskStatus:
    """現在のタスク状態を返す。

    ライブセッションがなければ、永続化された最新タスクを返す
    （ブラウザリロード後も状態が分かるように）。
    """
    if _active_session is not None:
        return _make_status(_active_session)
    latest = get_store().latest()
    if latest is not None:
        d = latest.to_dict()
        return TaskStatus(
            session_id=d.get("id"),
            state=d.get("state", "idle"),
            is_running=False,
            action_count=d.get("action_count", 0),
            success_count=d.get("success_count", 0),
            failure_count=d.get("failure_count", 0),
            subtasks=d.get("subtasks", []),
            current_subtask_index=d.get("current_subtask_index", 0),
        )
    return TaskStatus(
        session_id=None,
        state="idle",
        is_running=False,
        action_count=0,
        success_count=0,
        failure_count=0,
    )


@app.get("/tasks", response_model=list[TaskSummary])
async def list_tasks(limit: int = 20) -> list[TaskSummary]:
    """タスク履歴を新しい順に返す。"""
    return [_to_summary(r.to_dict()) for r in get_store().list(limit=limit)]


@app.get("/tasks/{task_id}", response_model=TaskDetail)
async def get_task(task_id: str) -> TaskDetail:
    """タスク詳細（操作履歴つき）を返す。リロード後のログ復元用。"""
    from fastapi import HTTPException

    rec = get_store().load(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="task not found")
    d = rec.to_dict()
    summary = _to_summary(d)
    return TaskDetail(
        **summary.model_dump(),
        actions=[StoredActionItem(**a) for a in d.get("actions", [])],
        subtasks=d.get("subtasks", []),
        current_subtask_index=d.get("current_subtask_index", 0),
        goal=d.get("goal", {}),
        created_at=d.get("created_at", 0.0),
    )


@app.delete("/tasks/{task_id}")
async def delete_task(task_id: str) -> dict[str, str]:
    """タスク履歴を1件削除する。実行中なら先に停止する。"""
    from fastapi import HTTPException

    global _active_session

    if _active_session and _active_session.id == task_id:
        _active_session.stop()
        _active_session = None

    if not get_store().delete(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    return {"status": "deleted"}


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


# ── VM 管理（デバッグ用） ─────────────────────────────


@app.get("/vm/status", response_model=VmStatus)
async def vm_status() -> VmStatus:
    """VMコンテナの状態を返す。Docker未利用時は 503。"""
    from fastapi import HTTPException

    try:
        info = _get_vm_controller().status()
    except DockerUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return VmStatus(running=info.running, status=info.status, health=info.health, name=info.name)


@app.post("/vm/restart", response_model=VmStatus)
async def vm_restart() -> VmStatus:
    """VMコンテナを再起動する（ゲストOSごと作り直し）。

    実行中のエージェントタスクがあれば先に停止する。
    再起動自体は即時戻り、デスクトップが使えるまで数分かかる。
    """
    from fastapi import HTTPException

    global _active_session

    if _active_session and _active_session.is_running:
        _active_session.stop()
        _active_session = None

    try:
        info = _get_vm_controller().restart()
    except DockerUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return VmStatus(running=info.running, status=info.status, health=info.health, name=info.name)


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

    # セッションのイベントを WebSocket に転送する。
    # 先頭引数にセッションを受け取り、送信先の選別用IDを付与する。
    async def on_state(session, state, ctx):
        with contextlib.suppress(Exception):
            await ws.send_json(
                {
                    "type": "state",
                    "session_id": session.id,
                    "vm_id": getattr(session, "vm_id", None),
                    "state": state.value,
                    "subtask_index": ctx.current_subtask_index,
                    "subtask_count": len(ctx.subtasks),
                    "action_count": len(ctx.action_history),
                    "subtasks": _subtask_list(ctx),
                }
            )

    async def on_action(session, action, success):
        with contextlib.suppress(Exception):
            await ws.send_json(
                {
                    "type": "action",
                    "session_id": session.id,
                    "vm_id": getattr(session, "vm_id", None),
                    "action_type": action.action_type.value,
                    "description": action.description,
                    "success": success,
                }
            )

    async def on_error(session, error):
        with contextlib.suppress(Exception):
            await ws.send_json(
                {
                    "type": "error",
                    "session_id": session.id,
                    "vm_id": getattr(session, "vm_id", None),
                    "message": error,
                }
            )

    async def on_complete(session, success):
        with contextlib.suppress(Exception):
            await ws.send_json(
                {
                    "type": "complete",
                    "session_id": session.id,
                    "vm_id": getattr(session, "vm_id", None),
                    "success": success,
                }
            )

    # グローバルレジストリに登録（常に）
    _ws_state_cbs.append(on_state)
    _ws_action_cbs.append(on_action)
    _ws_error_cbs.append(on_error)
    _ws_complete_cbs.append(on_complete)

    # 現在アクティブなセッションにも登録
    if _active_session:
        _forward_registrations(_active_session)

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


async def _fanout(cbs: list, *args) -> None:
    """WSレジストリの全コールバックに配送する。"""
    for cb in list(cbs):
        if asyncio.iscoroutinefunction(cb):
            await cb(*args)
        else:
            cb(*args)


def _forward_registrations(session: TaskSession) -> None:
    """セッションのイベントをWSレジストリへ転送するラッパを登録する。"""

    async def _st(st, cx):
        await _fanout(_ws_state_cbs, session, st, cx)

    async def _ac(a, ok):
        await _fanout(_ws_action_cbs, session, a, ok)

    async def _er(e):
        await _fanout(_ws_error_cbs, session, e)

    async def _co(ok):
        await _fanout(_ws_complete_cbs, session, ok)

    session.on_state_change(_st)
    session.on_action(_ac)
    session.on_error(_er)
    session.on_complete(_co)


def _make_status(session: TaskSession) -> TaskStatus:
    ctx = session.loop.context
    return TaskStatus(
        session_id=session.id,
        state=session.loop.state.value,
        is_running=session.is_running,
        action_count=len(ctx.action_history),
        success_count=ctx.success_count,
        failure_count=ctx.failure_count,
        subtasks=[{"id": s.id, "description": s.description} for s in ctx.subtasks],
        current_subtask_index=ctx.current_subtask_index,
        vm_id=getattr(session, "vm_id", None),
    )


def _subtask_list(ctx) -> list[dict]:
    """WS配信用のサブタスク一覧。"""
    return [{"id": s.id, "description": s.description} for s in ctx.subtasks]
