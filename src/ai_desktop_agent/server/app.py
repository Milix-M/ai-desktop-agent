"""FastAPI アプリケーション — AI Desktop Agent のバックエンドサーバー。

WebSocket でフロントエンドと通信し、TaskSession を管理する。
フロントエンドは Next.js で別途配信される。
"""

import asyncio
import contextlib
import inspect
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import wraps
from typing import Annotated

from fastapi import FastAPI, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints

from ai_desktop_agent.server.auth import (
    COOKIE_NAME,
    SESSION_SECONDS,
    AuthenticationMiddleware,
    get_access,
)
from ai_desktop_agent.server.browser_access import ALLOWED_ORIGINS, BrowserOriginMiddleware
from ai_desktop_agent.server.persistence import StorageError
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.server.vm_control import (
    DockerUnavailableError,
    get_vm_controller,
)
from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.vm.async_io import run_display_call, run_vm_call
from ai_desktop_agent.vm.base import DisplayBackend
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


def _default_create_session(display: DisplayBackend | None = None) -> TaskSession:
    return TaskSession(store=get_store(), display=display)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """起動時に中断タスクをマークし、Android抑止タスクを開始する。"""
    global _active_session
    import faulthandler
    import signal

    # 固着診断用：SIGUSR1 で全スレッドのスタックをdocker logsへ出す（プロセスは継続）。
    # seccompでptrace/py-spyが使えない環境向け。
    with contextlib.suppress(Exception):
        faulthandler.register(signal.SIGUSR1)
    marked = get_store().mark_interrupted()
    get_access()
    await run_vm_call(get_pool().retire_legacy_relays)
    if marked:
        logger.info("起動時に中断タスク %d 件をマーク", marked)
    from ai_desktop_agent.server.android_provision import AndroidProvisioner

    def _android_busy(vm_id: str) -> bool:
        for sess in list(_sessions.values()):
            try:
                if getattr(sess, "vm_id", None) == vm_id and sess.is_running:
                    return True
            except Exception:
                continue
        if _active_session is not None:
            try:
                if getattr(_active_session, "vm_id", None) == vm_id and _active_session.is_running:
                    return True
            except Exception:
                pass
        return False

    provisioner = AndroidProvisioner(is_running_check=_android_busy)
    await provisioner.start()
    try:
        yield
    finally:
        await provisioner.stop()
        sessions = list(_sessions.values())
        for session in sessions:
            session.stop()
        await asyncio.gather(
            *(s._task for s in sessions if s._task is not None), return_exceptions=True
        )
        await asyncio.gather(
            *(s._cleanup_task for s in sessions if s._cleanup_task is not None),
            return_exceptions=True,
        )
        await asyncio.gather(*(s.aclose() for s in sessions), return_exceptions=True)
        _sessions.clear()
        _active_session = None
        await asyncio.to_thread(VNCClient.shutdown_runtime)


app = FastAPI(title="AI Desktop Agent", version="0.1.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def invalid_request_handler(request: Request, error: RequestValidationError):
    # Never echo credentials or invalid Unicode from submitted values into errors.
    detail = [{key: item[key] for key in ("loc", "msg", "type")} for item in error.errors()]
    return JSONResponse(
        status_code=422, content={"detail": detail}, headers={"Cache-Control": "no-store"}
    )


@app.exception_handler(StorageError)
async def storage_error_handler(request, error: StorageError):
    return JSONResponse(status_code=503, content={"detail": str(error)})


app.add_middleware(AuthenticationMiddleware)

# CORS: Next.js (port 3000) からの API 呼び出しを許可
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(BrowserOriginMiddleware)


class LoginRequest(BaseModel):
    key: str = Field(min_length=1, max_length=256)


@app.get("/auth/status")
async def auth_status(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": get_access().authenticate(request)[0]}


@app.post("/auth/login")
async def login(request: Request, body: LoginRequest, response: Response):
    from fastapi import HTTPException

    access = get_access()
    if not access.valid_key(body.key):
        raise HTTPException(
            status_code=401, detail="接続キーが違います", headers={"Cache-Control": "no-store"}
        )
    access.revoke(request.cookies.get(COOKIE_NAME, ""))
    response.set_cookie(
        COOKIE_NAME,
        access.new_session(),
        max_age=SESSION_SECONDS,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": True}


@app.post("/auth/logout")
async def logout(request: Request, response: Response):
    get_access().revoke(request.cookies.get(COOKIE_NAME, ""))
    response.delete_cookie(COOKIE_NAME, httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return {"status": "logged_out"}


@app.websocket("/vnc/{vm_id}")
async def vnc_stream(ws: WebSocket, vm_id: str):
    from fastapi import HTTPException

    from ai_desktop_agent.server.vnc_proxy import proxy_vnc

    try:
        vm = await run_vm_call(_resolve_vm, vm_id)
    except (HTTPException, DockerUnavailableError, StorageError):
        await ws.close(1008)
        return
    await proxy_vnc(ws, vm.vnc_host or "vm")


# アクティブなセッション（後方互換：最新の1件）
_active_session: TaskSession | None = None

# 全セッション（複数VMの並列実行用）： session_id -> TaskSession
_sessions: dict[str, TaskSession] = {}
_task_start_lock = asyncio.Lock()

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
    instruction: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)
    ]
    vm_id: str | None = None  # 省略時は稼働中の既定VM
    allow_vm_restart: bool = False  # エージェント判断でのVM再起動を許可


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
    revision: int = 0


class VmStatus(BaseModel):
    running: bool
    status: str
    health: str | None = None
    name: str | None = None
    qmp_status: str | None = None


class VmInfoModel(BaseModel):
    id: str
    name: str
    status: str
    health: str | None = None
    vnc_port: int = 0
    ws_port: int = 0
    vnc_host: str = ""
    managed: bool = True
    kind: str = "qemu"
    adb_port: int = 0


class CreateVmRequest(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    kind: str | None = None  # qemu（既定） / container / android。省略時は qemu


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
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    decision_calls: int = 0
    decision_escalations: int = 0


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
        prompt_tokens=d.get("prompt_tokens", 0),
        completion_tokens=d.get("completion_tokens", 0),
        llm_calls=d.get("llm_calls", 0),
        decision_calls=d.get("decision_calls", 0),
        decision_escalations=d.get("decision_escalations", 0),
    )


# テスト用の VM コントローラファクトリ。テストから差し替え可能。
_get_vm_controller = get_vm_controller


# ── REST API ──────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _serialize_task_submission(fn):
    @wraps(fn)
    async def submit(*args, **kwargs):
        # Serialize handover only; already-started tasks on other VMs keep running.
        async with _task_start_lock:
            return await fn(*args, **kwargs)

    return submit


@app.post("/tasks", response_model=TaskStatus)
@_serialize_task_submission
async def create_task(req: CreateTaskRequest) -> TaskStatus:
    """新しいタスクを作成し、バックグラウンドで実行開始する。

    vm_id 省略時は稼働中の既定VMを使う。複数VMで並列実行できる。
    """
    global _active_session

    await asyncio.to_thread(get_store().check_writable)

    vm = await run_vm_call(_resolve_vm, req.vm_id)
    display = await run_display_call(_connect_vm_display, vm)

    previous = [s for s in _sessions.values() if s.vm_id == vm.id and s.is_running]
    for running in previous:
        running.stop()
    if previous:
        await asyncio.gather(*(s._task for s in previous if s._task), return_exceptions=True)

    try:
        session = await run_display_call(_create_session, display)
    except BaseException:
        await run_display_call(display.disconnect)
        raise
    session.vm_id = vm.id
    session.allow_vm_restart = req.allow_vm_restart

    # 全 WebSocket 接続にセッション情報を付与して配送する
    _forward_registrations(session)

    _active_session = session
    _sessions[session.id] = session

    await session.start_async(req.instruction)
    if session._task is not None:
        session._task.add_done_callback(lambda _: _sessions.pop(session.id, None))
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
    """環境へのVNC接続を作る（Android-x86もQEMU/VNCのため同一）。"""
    from fastapi import HTTPException

    try:
        display = VNCClient()
        display.connect(vm.vnc_host or "vm", 5900)
        return display
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"環境へのVNC接続に失敗: {e}") from e


@app.get("/vms", response_model=list[VmInfoModel])
async def list_vms() -> list[VmInfoModel]:
    """VM一覧を返す。"""
    from fastapi import HTTPException

    try:
        return [VmInfoModel(**v.to_dict()) for v in await run_vm_call(get_pool().list_vms)]
    except DockerUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.post("/vms", response_model=VmInfoModel)
async def create_vm(req: CreateVmRequest) -> VmInfoModel:
    """新しい環境を作成して起動する。kind=qemu（既定・KVM必須）/ container。

    KVM非対応環境での qemu 作成は409。container はどこでも作れる。
    """
    from fastapi import HTTPException

    from ai_desktop_agent.server.kvm import KvmUnavailableError

    kind = (req.kind or "qemu").lower()
    if kind not in ("qemu", "container", "android"):
        raise HTTPException(status_code=400, detail="kind は qemu/container/android のいずれか")
    try:
        info = await run_vm_call(get_pool().create_vm, name=req.name, kind=kind)
    except DockerUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except KvmUnavailableError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return VmInfoModel(**info.to_dict())


@app.delete("/vms/{vm_id}")
@_serialize_task_submission
async def delete_vm(vm_id: str) -> dict[str, str]:
    """VM/コンテナを削除する（上のタスクは停止する）。

    動的環境＋固定 vm/desktop/android が対象。
    固定androidと同一ラベルを持つ手動コンテナも対象になる点に注意。
    存在しなければ404。
    """
    from fastapi import HTTPException

    await _stop_sessions_for_vm(vm_id)

    try:
        ok = await run_vm_call(get_pool().remove_vm, vm_id)
    except DockerUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=404, detail="vm not found")
    return {"status": "deleted"}


@app.post("/vms/{vm_id}/stop", response_model=VmInfoModel)
@_serialize_task_submission
async def stop_vm(vm_id: str) -> VmInfoModel:
    """VM/コンテナを停止する（固定・動的とも可）。上のタスクは停止する。"""
    from fastapi import HTTPException

    await _stop_sessions_for_vm(vm_id)

    try:
        info = await run_vm_call(get_pool().stop_vm, vm_id)
    except DockerUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return VmInfoModel(**info.to_dict())


@app.post("/vms/{vm_id}/start", response_model=VmInfoModel)
@_serialize_task_submission
async def start_vm(vm_id: str) -> VmInfoModel:
    """VM/コンテナを起動する（固定・動的とも可）。

    KVM非対応環境での QEMU系（VM・Android）起動は409。
    """
    from fastapi import HTTPException

    from ai_desktop_agent.server.kvm import KvmUnavailableError

    try:
        info = await run_vm_call(get_pool().start_vm, vm_id)
    except DockerUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except KvmUnavailableError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return VmInfoModel(**info.to_dict())


async def _stop_sessions_for_vm(vm_id: str) -> None:
    """指定VMに紐づく実行中セッションを停止する。"""
    global _active_session

    sessions = [s for s in _sessions.values() if getattr(s, "vm_id", None) == vm_id]
    for sess in sessions:
        sess.stop()
    await asyncio.gather(
        *(s._task for s in sessions if s._task is not None), return_exceptions=True
    )
    for sess in sessions:
        _sessions.pop(sess.id, None)
    if _active_session and getattr(_active_session, "vm_id", None) == vm_id:
        _active_session = None


@app.get("/tasks/current", response_model=TaskStatus)
async def get_current_task(vm_id: str | None = None) -> TaskStatus:
    """現在のタスク状態を返す。

    ライブセッションがなければ、永続化された最新タスクを返す
    （ブラウザリロード後も状態が分かるように）。
    """
    if vm_id is not None:
        statuses = await _task_snapshot()
        match = next((s for s in statuses if s.vm_id == vm_id), None)
        if match is not None:
            return match
    elif _active_session is not None:
        return _make_status(_active_session)
    latest = await asyncio.to_thread(get_store().latest) if vm_id is None else None
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
            vm_id=d.get("vm_id"),
            revision=d.get("revision", 0),
        )
    return TaskStatus(
        session_id=None,
        state="idle",
        is_running=False,
        action_count=0,
        success_count=0,
        failure_count=0,
        vm_id=vm_id,
    )


@app.get("/tasks", response_model=list[TaskSummary])
async def list_tasks(limit: int = Query(default=20, ge=1, le=1000)) -> list[TaskSummary]:
    """タスク履歴を新しい順に返す。"""
    records = await asyncio.to_thread(get_store().list, limit=limit)
    return [_to_summary(r.to_dict()) for r in records]


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

    session = _sessions.get(task_id)
    if session is None and _active_session and _active_session.id == task_id:
        session = _active_session
    if session is not None:
        session.stop()
        if session._task is not None:
            await asyncio.gather(session._task, return_exceptions=True)
        _sessions.pop(task_id, None)
    if _active_session is session:
        _active_session = None

    if not get_store().delete(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    return {"status": "deleted"}


def _control_session(vm_id: str | None) -> TaskSession | None:
    if vm_id is None:
        return _active_session
    return next(
        (s for s in reversed(list(_sessions.values())) if s.vm_id == vm_id and s.is_running),
        None,
    )


@app.post("/tasks/current/pause")
async def pause_task(vm_id: str | None = None) -> dict[str, str]:
    session = _control_session(vm_id)
    if session:
        session.pause()
        return {"status": "paused"}
    return {"status": "no_session"}


@app.post("/tasks/current/resume")
async def resume_task(vm_id: str | None = None) -> dict[str, str]:
    session = _control_session(vm_id)
    if session:
        session.resume()
        return {"status": "resumed"}
    return {"status": "no_session"}


@app.post("/tasks/current/stop")
async def stop_task(vm_id: str | None = None) -> dict[str, str]:
    session = _control_session(vm_id)
    if session:
        session.stop()
        return {"status": "stopped"}
    return {"status": "no_session"}


# ── VM 管理（デバッグ用） ─────────────────────────────


@app.get("/vm/status", response_model=VmStatus)
async def vm_status() -> VmStatus:
    """VMコンテナの状態を返す。Docker未利用時は 503。"""
    from fastapi import HTTPException

    try:
        info = await run_vm_call(_get_vm_controller().status)
    except DockerUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return VmStatus(
        running=info.running,
        status=info.status,
        health=info.health,
        name=info.name,
        qmp_status=info.qmp_status,
    )


@app.post("/vm/restart", response_model=VmStatus)
@_serialize_task_submission
async def vm_restart() -> VmStatus:
    """VMコンテナを再起動する（ゲストOSを含む再起動）。

    実行中のエージェントタスクがあれば先に停止する。
    再起動自体は即時戻り、デスクトップが使えるまで数分かかる。
    KVMが利用できない環境では409（コンテナ環境を使用すること）。
    """
    from fastapi import HTTPException

    from ai_desktop_agent.server.kvm import KvmUnavailableError

    controller = _get_vm_controller()
    target_sessions = [s for s in _sessions.values() if s.vm_id == "vm" and s.is_running]
    if (
        _active_session is not None
        and _active_session.vm_id == "vm"
        and _active_session.is_running
        and _active_session not in target_sessions
    ):
        target_sessions.append(_active_session)
    for session in target_sessions:
        session.stop()
    await asyncio.gather(
        *(s._task for s in target_sessions if s._task is not None), return_exceptions=True
    )

    try:
        info = await run_vm_call(controller.restart)
    except DockerUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except KvmUnavailableError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return VmStatus(
        running=info.running,
        status=info.status,
        health=info.health,
        name=info.name,
        qmp_status=info.qmp_status,
    )


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
    outbound: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
    overloaded = False

    def enqueue(payload: dict) -> None:
        nonlocal overloaded
        if overloaded:
            return
        try:
            outbound.put_nowait(payload)
        except asyncio.QueueFull:
            overloaded = True

    async def deliver() -> None:
        while True:
            payload = await outbound.get()
            if overloaded or not await _send_json(ws, payload):
                with contextlib.suppress(Exception):
                    await ws.close(code=1013)
                return

    # セッションのイベントを WebSocket に転送する。
    # 先頭引数にセッションを受け取り、送信先の選別用IDを付与する。
    # 送信は5秒で打ち切る（死んだ接続へのsend_json無限待ちで
    # セッション全体が固まるのを防ぐ。実測で発生済み）。
    def on_state(session, state, ctx):
        enqueue(
            {
                "type": "state",
                "session_id": session.id,
                "vm_id": getattr(session, "vm_id", None),
                "state": state.value,
                "revision": session.loop.transition_count,
                "subtask_index": ctx.current_subtask_index,
                "subtask_count": len(ctx.subtasks),
                "action_count": len(ctx.action_history),
                "subtasks": _subtask_list(ctx),
            },
        )

    def on_action(session, action, success):
        enqueue(
            {
                "type": "action",
                "session_id": session.id,
                "vm_id": getattr(session, "vm_id", None),
                "action_type": action.action_type.value,
                "description": action.description,
                "success": success,
            },
        )

    def on_error(session, error):
        enqueue(
            {
                "type": "error",
                "session_id": session.id,
                "vm_id": getattr(session, "vm_id", None),
                "message": error,
            },
        )

    def on_complete(session, success):
        enqueue(
            {
                "type": "complete",
                "session_id": session.id,
                "vm_id": getattr(session, "vm_id", None),
                "success": success,
                "revision": session.loop.transition_count,
            },
        )

    # グローバルレジストリに登録（常に）
    _ws_state_cbs.append(on_state)
    _ws_action_cbs.append(on_action)
    _ws_error_cbs.append(on_error)
    _ws_complete_cbs.append(on_complete)

    # 現在アクティブなセッションにも登録
    if _active_session:
        _forward_registrations(_active_session)

    delivery: asyncio.Task | None = None
    try:
        enqueue({"type": "snapshot", "tasks": [s.model_dump() for s in await _task_snapshot()]})
        delivery = asyncio.create_task(deliver())
        while True:
            data = await ws.receive_text()
            if data == "ping":
                enqueue({"type": "pong"})
    except WebSocketDisconnect:
        logger.info("WebSocket 切断")
    except Exception:
        pass
    finally:
        if delivery is not None:
            delivery.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await delivery
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


async def _task_snapshot() -> list[TaskStatus]:
    """Return the latest task for every VM, including tasks finished while disconnected."""
    records = await asyncio.to_thread(get_store().list, limit=None)
    statuses: dict[str, TaskStatus] = {}
    for record in records:
        if record.vm_id is None or record.vm_id in statuses:
            continue
        statuses[record.vm_id] = TaskStatus(
            session_id=record.id,
            vm_id=record.vm_id,
            state=record.state,
            is_running=False,
            action_count=len(record.actions),
            success_count=sum(a.success for a in record.actions),
            failure_count=sum(not a.success for a in record.actions),
            subtasks=record.subtasks,
            current_subtask_index=record.current_subtask_index,
            revision=record.revision,
        )
    sessions = list(_sessions.values())
    if _active_session is not None and _active_session not in sessions:
        sessions.append(_active_session)
    for session in sessions:
        if session.vm_id is not None:
            statuses[session.vm_id] = _make_status(session)
    return list(statuses.values())


async def _send_json(ws: WebSocket, payload: dict, timeout: float = 5.0) -> bool:
    """WebSocketへ送信する（打ち切り付き）。

    死んだ接続への ``send_json`` 無限待ちでセッション全体が固まるのを防ぐ。
    タイムアウト・送信エラーは黙って捨てる（切断検出は受信側で行う）。
    """
    try:
        await asyncio.wait_for(ws.send_json(payload), timeout=timeout)
        return True
    except Exception:
        return False


async def _fanout(cbs: list, *args) -> None:
    """WSレジストリの全コールバックに配送する。"""
    for cb in list(cbs):
        result = cb(*args)
        if inspect.isawaitable(result):
            await result


def _forward_registrations(session: TaskSession) -> None:
    """セッションのイベントをWSレジストリへ転送するラッパを登録する。

    WS接続ごとに呼ばれるが、同一セッションへの重複登録は
    ログ二重表示の原因になるため初回のみ登録する。
    ラッパはグローバルレジストリを参照するため、1組あれば
    後から接続したWSにも配送される。
    """
    if getattr(session, "_ws_forwarded", False):
        return
    session._ws_forwarded = True  # type: ignore[attr-defined]

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
        state=(
            "understanding"
            if session.is_running and session.loop.state.value == "idle"
            else session.loop.state.value
        ),
        is_running=session.is_running,
        action_count=len(ctx.action_history),
        success_count=ctx.success_count,
        failure_count=ctx.failure_count,
        subtasks=[{"id": s.id, "description": s.description} for s in ctx.subtasks],
        current_subtask_index=ctx.current_subtask_index,
        vm_id=getattr(session, "vm_id", None),
        revision=session.loop.transition_count,
    )


def _subtask_list(ctx) -> list[dict]:
    """WS配信用のサブタスク一覧。"""
    return [{"id": s.id, "description": s.description} for s in ctx.subtasks]
