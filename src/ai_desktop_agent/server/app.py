"""FastAPI アプリケーション — AI Desktop Agent のバックエンドサーバー。

WebSocket でフロントエンドと通信し、TaskSession を管理する。
フロントエンドは Next.js で別途配信される。
"""

import asyncio
import contextlib
import inspect
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import wraps
from typing import Annotated

from anyio import CancelScope
from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints

from ai_desktop_agent.agent.budget import ExecutionLimits
from ai_desktop_agent.agent.output import TaskOutput
from ai_desktop_agent.privacy import install_log_redaction, redact_data
from ai_desktop_agent.server.auth import (
    COOKIE_NAME,
    SESSION_SECONDS,
    AuthenticationMiddleware,
    AuthenticationRateLimitError,
    authentication_rate_limit_response,
    get_access,
)
from ai_desktop_agent.server.browser_access import (
    ALLOWED_ORIGINS,
    BrowserOriginMiddleware,
    ManagementNetworkMiddleware,
)
from ai_desktop_agent.server.desktop_resources import DesktopResources, desktop_runtime_from_env
from ai_desktop_agent.server.persistence import DeletionError, StorageError
from ai_desktop_agent.server.request_limits import RequestBodyLimitMiddleware
from ai_desktop_agent.server.session import TaskSession, _drain_task
from ai_desktop_agent.server.store import TaskRecord, TaskStore
from ai_desktop_agent.server.submissions import (
    SubmissionClaim,
    SubmissionConflictError,
    SubmissionGoneError,
    SubmissionPendingError,
    fingerprint,
    key_digest,
)
from ai_desktop_agent.server.task_admission import TaskAdmissionMiddleware
from ai_desktop_agent.server.vm_control import (
    DockerUnavailableError,
    get_vm_controller,
)
from ai_desktop_agent.server.vm_creations import (
    CreationConflictError,
    CreationStore,
    VmCreationService,
)
from ai_desktop_agent.server.vm_locks import AsyncKeyedLocks
from ai_desktop_agent.server.vm_pool import VmPool
from ai_desktop_agent.server.webhook import TaskWebhook, open_task_webhook
from ai_desktop_agent.server.websocket_io import close_websocket
from ai_desktop_agent.server.workspaces import IDENTIFIER_PATTERN
from ai_desktop_agent.vm.async_io import (
    VmCreationBusyError,
    run_display_call,
    run_vm_call,
    run_vm_creation,
)
from ai_desktop_agent.vm.base import DisplayBackend
from ai_desktop_agent.vm.vnc_client import VNCClient

_server_id = uuid.uuid4().hex

logging.basicConfig(level=logging.INFO)
install_log_redaction()

logger = logging.getLogger(__name__)


# ── 永続化ストア（遅延初期化：テスト時は差し替え可能） ──

_store: TaskStore | None = None
_task_webhook: TaskWebhook | None = None


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
    """Own the shared data before recovery, and retain ownership through shutdown."""
    global _vm_creations, _pool

    ExecutionLimits.from_env()
    DesktopResources.from_env()
    desktop_runtime_from_env()
    creations = get_vm_creations()
    try:
        await creations.acquire_ownership()
        async with _owned_lifespan(creations):
            yield
    finally:

        async def close_services():
            try:
                await creations.close()
            finally:
                await run_vm_call(creations.pool.close)

        try:
            await _drain_task(asyncio.create_task(close_services()))
        finally:
            if _vm_creations is creations:
                _vm_creations = None
            if _pool is creations.pool:
                _pool = None


@asynccontextmanager
async def _owned_lifespan(creations: VmCreationService) -> AsyncIterator[None]:
    global _android_provisioner, _server_id, _task_webhook
    import faulthandler
    import signal

    _server_id = uuid.uuid4().hex
    # 固着診断用：SIGUSR1 で全スレッドのスタックをdocker logsへ出す（プロセスは継続）。
    # seccompでptrace/py-spyが使えない環境向け。
    with contextlib.suppress(Exception):
        faulthandler.register(signal.SIGUSR1)
    get_access()
    store = await run_display_call(get_store)
    await run_display_call(store.redact_existing)
    marked = await run_display_call(store.mark_interrupted)
    await run_vm_call(get_pool().retire_legacy_relays)
    await run_vm_call(get_pool().isolate_environments)
    if marked:
        logger.info("起動時に中断タスク %d 件をマーク", marked)
    from ai_desktop_agent.server.android_provision import AndroidProvisioner

    def _android_busy(vm_id: str) -> bool:
        for sess in list(_sessions.values()):
            try:
                if getattr(sess, "vm_id", None) == vm_id and sess.is_running:
                    return True
            except Exception:
                return True
        if _active_session is not None:
            try:
                if getattr(_active_session, "vm_id", None) == vm_id and _active_session.is_running:
                    return True
            except Exception:
                return True
        return False

    provisioner = AndroidProvisioner(pool=get_pool(), is_running_check=_android_busy)
    async with open_task_webhook() as webhook:
        _android_provisioner = provisioner
        _task_webhook = webhook
        try:
            await creations.start()
            await provisioner.start()
            yield
        finally:
            try:
                await _drain_task(asyncio.create_task(_shutdown_backend(provisioner)))
            finally:
                _task_webhook = None


async def _shutdown_backend(provisioner) -> None:
    global _active_session, _android_provisioner

    try:
        await provisioner.stop()
    finally:
        _android_provisioner = None
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
        await run_display_call(VNCClient.shutdown_runtime)


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
    return JSONResponse(status_code=503, content=redact_data({"detail": str(error)}))


@app.exception_handler(AuthenticationRateLimitError)
async def authentication_limit_handler(request, error: AuthenticationRateLimitError):
    return authentication_rate_limit_response(error)


@app.exception_handler(HTTPException)
async def http_error_handler(request, error: HTTPException):
    return JSONResponse(
        status_code=error.status_code,
        content=redact_data({"detail": error.detail}),
        headers=error.headers,
    )


app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(TaskAdmissionMiddleware)
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
app.add_middleware(ManagementNetworkMiddleware)


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
    if not access.verify_key(body.key):
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
        vm = await run_vm_call(_resolve_vm, vm_id, require_ready=False)
    except (HTTPException, DockerUnavailableError, StorageError):
        await ws.close(1008)
        return

    def input_authority():
        for session in _sessions.values():
            if (
                session.vm_id == vm.id
                and session.is_running
                and session._manual_resume is not None
                and not session._manual_resume.is_set()
            ):
                return session._input_authority
        return None

    await proxy_vnc(ws, vm.vnc_host or "vm", input_authority=input_authority)


# アクティブなセッション（後方互換：最新の1件）
_active_session: TaskSession | None = None

# 全セッション（複数VMの並列実行用）： session_id -> TaskSession
_sessions: dict[str, TaskSession] = {}
# Each in-flight history scan retains the sessions and deletions it observes.
# The entries are scoped to that scan, so finished sessions are not cached forever.
_snapshot_readers: dict[object, dict[str, TaskSession | None]] = {}
_vm_operations = AsyncKeyedLocks()
_task_submissions = AsyncKeyedLocks()
_android_provisioner = None

# テスト用のセッションファクトリ。テストから差し替え可能。
_create_session = _default_create_session  # type: ignore[var-annotated]

# VMプール（遅延初期化：テスト時は差し替え可能）
_pool: VmPool | None = None
_vm_creations: VmCreationService | None = None


def get_pool() -> VmPool:
    """VMプールを返す（初回利用時に初期化）。"""
    global _pool
    if _pool is None:
        _pool = VmPool()
    return _pool


def get_vm_creations() -> VmCreationService:
    global _vm_creations
    if _vm_creations is None:
        import os
        from pathlib import Path

        directory = Path(os.environ.get("DATA_DIR", "./data")) / "vm-creations"
        _vm_creations = VmCreationService(get_pool(), CreationStore(directory))
    return _vm_creations


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
    replace_running: bool = True


class TaskStatus(BaseModel):
    server_id: str = Field(default_factory=lambda: _server_id)
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
    session_order: int = 0
    manual_input: dict | None = None
    replayed: bool = False


class ResumeTaskRequest(BaseModel):
    manual_input_id: str | None = Field(default=None, max_length=64)
    allow_screen_share: bool = False


ControlSessionId = Annotated[
    str | None, Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
]


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


VmId = Annotated[str, StringConstraints(pattern=rf"^{IDENTIFIER_PATTERN}$")]


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
    stop_reason: str | None = None
    error_message: str = ""
    session_order: int = 0


class TaskDetail(TaskSummary):
    result: list[TaskOutput] = []
    actions: list[StoredActionItem] = []
    subtasks: list[dict] = []
    current_subtask_index: int = 0
    goal: dict = {}
    created_at: float = 0.0
    execution_limits: dict = {}
    execution_usage: dict = {}


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
        stop_reason=d.get("stop_reason"),
        error_message=d.get("error_message", ""),
        session_order=d.get("session_order", 0),
    )


# テスト用の VM コントローラファクトリ。テストから差し替え可能。
_get_vm_controller = get_vm_controller


# ── REST API ──────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@asynccontextmanager
async def _suspend_android_provisioning(vm_id: str | None):
    provisioner = _android_provisioner
    if provisioner is None or vm_id is None:
        yield
    else:
        async with provisioner.suspend(vm_id):
            yield


@asynccontextmanager
async def _vm_operation(vm_id: str):
    async with _vm_operations.hold(vm_id), _suspend_android_provisioning(vm_id):
        yield


def _serialize_vm_operation(fn):
    @wraps(fn)
    async def submit(*args, **kwargs):
        vm_id = kwargs.get("vm_id", args[0] if args else None)
        async with _vm_operation(vm_id):
            return await fn(*args, **kwargs)

    return submit


@app.post("/tasks", response_model=TaskStatus)
async def create_task(
    req: CreateTaskRequest,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
    ] = None,
) -> TaskStatus:
    """新しいタスクを作成し、バックグラウンドで実行開始する。

    vm_id 省略時は稼働中の既定VMを使う。複数VMで並列実行できる。
    """
    if idempotency_key is None:
        return await _create_new_task(req)
    try:
        async with (
            _task_submissions.hold(key_digest(idempotency_key)),
            _submission_claim(idempotency_key, req) as claim,
        ):
            if claim.record is not None:
                live = _sessions.get(claim.task_id)
                if live is None and _active_session and _active_session.id == claim.task_id:
                    live = _active_session
                status = _make_status(live) if live is not None else _stored_status(claim.record)
                return status.model_copy(update={"replayed": True})
            return await _create_new_task(req, claim)
    except SubmissionConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except SubmissionPendingError as error:
        raise HTTPException(
            status_code=409, detail=str(error), headers={"Retry-After": "1"}
        ) from error
    except SubmissionGoneError as error:
        raise HTTPException(status_code=410, detail=str(error)) from error


@asynccontextmanager
async def _submission_claim(key: str, req: CreateTaskRequest):
    claim = None

    def acquire():
        nonlocal claim
        payload = req.model_dump()
        if req.replace_running:
            # Preserve fingerprints written before replacement was configurable.
            del payload["replace_running"]
        claim = get_store().claim_submission(key_digest(key), fingerprint(payload))

    try:
        await run_display_call(acquire)
        yield claim
    finally:
        if claim is not None:
            try:
                session = claim.session
                if session is not None and session._task is not None:
                    # An abandoned HTTP request does not cancel its background task.
                    # Retain the claim until its initial durable write has finished.
                    await _drain_task(
                        asyncio.create_task(session._started.wait()), ignore_cancellation=True
                    )
            finally:
                await run_display_call(claim.close)


async def _create_new_task(
    req: CreateTaskRequest, claim: SubmissionClaim | None = None
) -> TaskStatus:
    await run_display_call(get_store().check_writable)

    try:
        vm_id = req.vm_id or await run_vm_call(_default_vm_id)
        async with _vm_operation(vm_id):
            # Revalidate after waiting; a stop or deletion may have finished meanwhile.
            vm = await run_vm_call(_resolve_vm, vm_id)
            return await _start_task_on_vm(req, vm, claim)
    except DockerUnavailableError as error:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail=str(error)) from error


async def _start_task_on_vm(req, vm, claim: SubmissionClaim | None = None):
    global _active_session

    previous = [s for s in _sessions.values() if s.vm_id == vm.id and s.is_running]
    if previous and not req.replace_running:
        raise HTTPException(status_code=409, detail="この環境で別のタスクを実行中です")
    _check_task_replacement(previous)
    display = None
    session = None

    def connect():
        nonlocal display
        display = _connect_vm_display(vm)

    def create():
        nonlocal session
        session = _create_session(display)

    try:
        # Keep ownership even if cancellation discards the worker's return value.
        # run_display_call drains the worker before this scope releases anything.
        await run_display_call(connect)
        # The running task may start a manual handoff while VNC is connecting.
        _check_task_replacement(previous)
        for running in previous:
            running.stop()
        if previous:
            await _drain_task(
                asyncio.gather(*(s._task for s in previous if s._task), return_exceptions=True)
            )
        await run_display_call(create)
        if claim is not None:
            session.bind_submission(claim, get_store())
        await session.reserve_start_order()
    except BaseException:
        if session is not None:
            await session.aclose()
        elif display is not None:
            await run_display_call(display.disconnect)
        raise
    session.vm_id = vm.id
    session.allow_vm_restart = req.allow_vm_restart

    webhook = _task_webhook
    if webhook is not None:
        store = get_store()

        async def notify_webhook(success: bool) -> None:
            await webhook.task_finished(store, session.id, success)

        session.on_complete(notify_webhook)

    # 全 WebSocket 接続にセッション情報を付与して配送する
    _forward_registrations(session)

    if _active_session is None or session.session_order >= _active_session.session_order:
        _active_session = session
    _sessions[session.id] = session
    _remember_snapshot_change(session.id, session)

    try:
        await _drain_task(asyncio.create_task(session.start_async(req.instruction)))
    finally:
        if session._task is not None:
            session._task.add_done_callback(lambda _: _sessions.pop(session.id, None))
    return _make_status(session)


def _check_task_replacement(sessions):
    if any(isinstance(getattr(session, "manual_input", None), dict) for session in sessions):
        raise HTTPException(
            status_code=409, detail="手動入力を終えて再開するか、タスクを停止してください"
        )


def _default_vm_id() -> str:
    vm = get_pool().default_vm()
    if vm is None:
        raise HTTPException(status_code=409, detail="利用可能なVMがありません")
    return vm.id


def _resolve_vm(vm_id: str | None, *, require_ready: bool = True):
    """タスク投入先のVMを解決する。"""

    from ai_desktop_agent.server.vm_locks import environment_lock

    pool = get_pool()
    if not vm_id:
        vm_id = _default_vm_id()
    with environment_lock(vm_id):
        return _resolve_named_vm(pool, vm_id, require_ready=require_ready)


def _resolve_named_vm(pool, vm_id: str, *, require_ready: bool):
    from fastapi import HTTPException

    vm = pool.get_vm(vm_id)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"vm not found: {vm_id}")
    if vm.status != "running":
        raise HTTPException(status_code=409, detail=f"vm not running: {vm_id}")
    if require_ready:
        from ai_desktop_agent.server.readiness import is_ready

        if not is_ready(vm.status, vm.health):
            detail = (
                "環境は起動準備中です"
                if vm.health == "starting"
                else "環境の接続状態を確認してください"
            )
            raise HTTPException(status_code=409, detail=detail)
    pool.prepare_vm(vm.id)
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
        await run_vm_call(get_pool().isolate_environments)
        return [VmInfoModel(**v.to_dict()) for v in await run_vm_call(get_pool().list_vms)]
    except DockerUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.post("/vms", response_model=VmInfoModel)
async def create_vm(
    req: CreateVmRequest,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
    ] = None,
) -> VmInfoModel:
    """新しい環境を作成して起動する。kind=qemu（既定・KVM必須）/ container。

    KVM非対応環境での qemu 作成は409。container はどこでも作れる。
    """
    from fastapi import HTTPException

    from ai_desktop_agent.server.kvm import KvmUnavailableError

    kind = (req.kind or "qemu").lower()
    if kind not in ("qemu", "container", "android"):
        raise HTTPException(status_code=400, detail="kind は qemu/container/android のいずれか")
    try:
        if idempotency_key is None:
            info = await run_vm_creation(get_pool().create_vm, name=req.name, kind=kind)
        else:
            service = get_vm_creations()
            accepted = await service.submit(idempotency_key, req.name, kind)
            finished = await service.wait(accepted.id)
            if finished is None:
                raise HTTPException(status_code=503, detail="環境作成の記録を確認できません")
            if finished.state == "failed":
                raise HTTPException(status_code=finished.error_status, detail=finished.error)
            info = finished.result
            if info is None:
                raise HTTPException(status_code=503, detail="環境作成の結果を確認できません")
    except CreationConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except VmCreationBusyError as e:
        raise HTTPException(status_code=429, detail=str(e), headers={"Retry-After": "1"}) from e
    except DockerUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except KvmUnavailableError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return VmInfoModel(**info.to_dict())


@app.post("/vm-creations")
async def submit_vm_creation(
    req: CreateVmRequest,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
    ],
) -> JSONResponse:
    kind = (req.kind or "qemu").lower()
    try:
        record = await get_vm_creations().submit(idempotency_key, req.name, kind)
    except CreationConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except VmCreationBusyError as error:
        raise HTTPException(
            status_code=429, detail=str(error), headers={"Retry-After": "1"}
        ) from error
    except DockerUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return JSONResponse(
        status_code=202 if record.state in {"queued", "running"} else 200,
        content=record.public(),
        headers={"Location": "/vm-creations/" + record.id, "Cache-Control": "no-store"},
    )


@app.get("/vm-creations/{job_id}")
async def get_vm_creation(job_id: str) -> JSONResponse:
    try:
        record = await get_vm_creations().get(job_id)
    except VmCreationBusyError as error:
        raise HTTPException(
            status_code=429, detail=str(error), headers={"Retry-After": "1"}
        ) from error
    except DockerUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=404, detail="作成要求が見つかりません") from error
    if record is None:
        raise HTTPException(status_code=404, detail="作成要求が見つかりません")
    return JSONResponse(content=record.public(), headers={"Cache-Control": "no-store"})


@app.delete("/vms/{vm_id}")
@_serialize_vm_operation
async def delete_vm(vm_id: VmId) -> dict[str, str]:
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
        raise HTTPException(status_code=503, detail="Dockerの応答を確認してください") from e
    if not ok:
        raise HTTPException(status_code=404, detail="vm not found")
    return {"status": "deleted"}


@app.post("/vms/{vm_id}/stop", response_model=VmInfoModel)
@_serialize_vm_operation
async def stop_vm(vm_id: VmId) -> VmInfoModel:
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
@_serialize_vm_operation
async def start_vm(vm_id: VmId) -> VmInfoModel:
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
    if (
        _active_session is not None
        and _active_session.vm_id == vm_id
        and _active_session not in sessions
    ):
        sessions.append(_active_session)
    for sess in sessions:
        sess.stop()
    await _drain_task(
        asyncio.gather(*(s._task for s in sessions if s._task is not None), return_exceptions=True)
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
        return _stored_status(latest)
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
    records = await asyncio.to_thread(get_store().summaries, limit=limit)
    return [_to_summary(record) for record in records]


@app.get("/tasks/{task_id}", response_model=TaskDetail)
async def get_task(task_id: str) -> TaskDetail:
    """タスク詳細（操作履歴つき）を返す。リロード後のログ復元用。"""
    from fastapi import HTTPException

    rec = await asyncio.to_thread(get_store().load, task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="task not found")
    d = rec.to_dict()
    summary = _to_summary(d)
    return TaskDetail(
        **summary.model_dump(),
        result=rec.result,
        actions=[StoredActionItem(**a) for a in d.get("actions", [])],
        subtasks=d.get("subtasks", []),
        current_subtask_index=d.get("current_subtask_index", 0),
        goal=d.get("goal", {}),
        created_at=d.get("created_at", 0.0),
        execution_limits=d.get("execution_limits", {}),
        execution_usage=d.get("execution_usage", {}),
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
        task = session._task
        if task is None:
            raise HTTPException(
                status_code=409,
                detail="タスクを開始しています。開始後にもう一度削除してください",
                headers={"Retry-After": "1"},
            )
        session.stop()
        await asyncio.gather(task, return_exceptions=True)
        if session._cleanup_task is not None:
            await asyncio.shield(session._cleanup_task)
        _sessions.pop(task_id, None)
    if _active_session is session:
        _active_session = None

    deleted = False

    def remove():
        nonlocal deleted
        try:
            deleted = get_store().delete(task_id)
        except DeletionError as error:
            # A failed directory sync cannot restore an entry already unlinked.
            deleted = error.removed
            raise

    try:
        await run_display_call(remove)
    finally:
        # run_display_call drains the worker even when the HTTP request is cancelled.
        if deleted:
            _remember_snapshot_change(task_id, None)
    if not deleted:
        raise HTTPException(status_code=404, detail="task not found")
    return {"status": "deleted"}


def _control_session(vm_id: str | None, session_id: str | None = None) -> TaskSession | None:
    if vm_id is None:
        session = _active_session
    else:
        session = next(
            (s for s in reversed(list(_sessions.values())) if s.vm_id == vm_id and s.is_running),
            None,
        )
    if session_id is not None and (session is None or session.id != session_id):
        raise HTTPException(
            status_code=409,
            detail="操作対象のタスクは終了したか、切り替わりました。状態を確認してください",
        )
    # No await between this comparison and the caller's synchronous control method:
    # another submission cannot replace the session in the middle of the operation.
    return session


@app.post("/tasks/current/pause")
async def pause_task(
    vm_id: str | None = None, session_id: ControlSessionId = None
) -> dict[str, str]:
    session = _control_session(vm_id, session_id)
    if session:
        notification = session.pause()
        if inspect.isawaitable(notification):
            await asyncio.shield(notification)
        return {"status": "paused"}
    return {"status": "no_session"}


@app.post("/tasks/current/resume")
async def resume_task(
    vm_id: str | None = None,
    req: ResumeTaskRequest | None = None,
    session_id: ControlSessionId = None,
) -> dict[str, str]:
    session = _control_session(vm_id, session_id)
    if session:
        if isinstance(getattr(session, "manual_input", None), dict):
            from fastapi import HTTPException

            try:
                notification = session.resume(
                    manual_input_id=req.manual_input_id if req else None,
                    allow_screen_share=req.allow_screen_share if req else False,
                )
            except ValueError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
        else:
            notification = session.resume()
        if inspect.isawaitable(notification):
            await asyncio.shield(notification)
        return {"status": "resumed"}
    return {"status": "no_session"}


@app.post("/tasks/current/stop")
async def stop_task(
    vm_id: str | None = None, session_id: ControlSessionId = None
) -> dict[str, str]:
    session = _control_session(vm_id, session_id)
    if session:
        session.stop()
        return {"status": "stopped"}
    return {"status": "no_session"}


# ── VM 管理（デバッグ用） ─────────────────────────────


@app.get("/vm/status", response_model=VmStatus)
async def vm_status() -> VmStatus:
    """VMコンテナの状態を返す。Docker未利用時は 503。"""
    from fastapi import HTTPException

    async with _controller_connection() as controller:
        try:
            info = await run_vm_call(controller.status)
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
async def vm_restart() -> VmStatus:
    """VMコンテナを再起動する（ゲストOSを含む再起動）。

    実行中のエージェントタスクがあれば先に停止する。
    再起動自体は即時戻り、デスクトップが使えるまで数分かかる。
    KVMが利用できない環境では409（コンテナ環境を使用すること）。
    """
    from fastapi import HTTPException

    from ai_desktop_agent.server.kvm import KvmUnavailableError

    async with _controller_connection() as controller:
        try:
            vm_id = await run_vm_call(controller.target_vm_id)
            async with _vm_operation(vm_id):
                await _stop_sessions_for_vm(vm_id)
                info = await run_vm_call(controller.restart, expected_vm_id=vm_id)
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


@asynccontextmanager
async def _controller_connection():
    controller = _get_vm_controller()
    try:
        yield controller
    finally:
        close = getattr(controller, "close", None)
        if callable(close):
            await run_vm_call(close)


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
    server_id = _server_id
    overloaded = False

    def enqueue(payload: dict) -> None:
        nonlocal overloaded
        if overloaded:
            return
        if payload["type"] != "pong":
            payload = {**payload, "server_id": server_id}
        try:
            outbound.put_nowait(redact_data(payload))
        except asyncio.QueueFull:
            overloaded = True

    async def deliver() -> int:
        while True:
            payload = await outbound.get()
            if overloaded or not await _send_json(ws, payload):
                return 1013

    async def receive() -> int:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return 1000
            if message.get("text") != "ping":
                return 1003
            enqueue({"type": "pong"})

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
                "manual_input": getattr(session, "manual_input", None),
                "revision": session.loop.transition_count,
                "session_order": session.session_order,
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
                "session_order": session.session_order,
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
                "session_order": session.session_order,
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
                "session_order": session.session_order,
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

    pumps: list[asyncio.Task] = []
    close_code = 1000
    try:
        # Read the fence first: a task created during the scan must never be cleared
        # just because its initial state was not yet present in that scan.
        through_order = await asyncio.to_thread(get_store().current_session_order)
        enqueue(
            {
                "type": "snapshot",
                "through_session_order": through_order,
                "tasks": [s.model_dump() for s in await _task_snapshot()],
            }
        )
        pumps = [asyncio.create_task(deliver()), asyncio.create_task(receive())]
        done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            close_code = max(close_code, await task)
    except WebSocketDisconnect:
        logger.info("WebSocket 切断")
    except Exception:
        close_code = 1011
    finally:
        # ASGI hosts may cancel repeatedly while a client disconnects.
        with CancelScope(shield=True):
            for task in pumps:
                task.cancel()
            # 切断時にグローバルレジストリから除去
            with contextlib.suppress(ValueError):
                _ws_state_cbs.remove(on_state)
            with contextlib.suppress(ValueError):
                _ws_action_cbs.remove(on_action)
            with contextlib.suppress(ValueError):
                _ws_error_cbs.remove(on_error)
            with contextlib.suppress(ValueError):
                _ws_complete_cbs.remove(on_complete)
            await asyncio.gather(*pumps, return_exceptions=True)
            await close_websocket(ws, code=close_code)


# ── ヘルパー ──────────────────────────────────────────


def _stored_status(record: TaskRecord) -> TaskStatus:
    return TaskStatus(
        session_id=record.id,
        state=record.state,
        is_running=False,
        action_count=len(record.actions),
        success_count=sum(a.success for a in record.actions),
        failure_count=sum(not a.success for a in record.actions),
        subtasks=record.subtasks,
        current_subtask_index=record.current_subtask_index,
        vm_id=record.vm_id,
        revision=record.revision,
        session_order=record.session_order,
    )


def _remember_snapshot_change(task_id: str, session: TaskSession | None) -> None:
    for changes in _snapshot_readers.values():
        changes[task_id] = session


async def _task_snapshot() -> list[TaskStatus]:
    """Merge history with starts, completions and deletions occurring during its scan."""
    token = object()
    changes: dict[str, TaskSession | None] = {s.id: s for s in _sessions.values()}
    if _active_session is not None:
        changes.setdefault(_active_session.id, _active_session)
    _snapshot_readers[token] = changes
    try:
        while True:
            records = await asyncio.to_thread(get_store().latest_per_vm)
            # Removing a selected record exposes the preceding task on that VM.
            # Keep tracking changes during the reread, including another deletion.
            if not any(
                record["id"] in changes and changes[record["id"]] is None for record in records
            ):
                break
        for session in _sessions.values():
            changes.setdefault(session.id, session)
        if _active_session is not None:
            changes.setdefault(_active_session.id, _active_session)
        deleted = {task_id for task_id, session in changes.items() if session is None}
        statuses: dict[str, TaskStatus] = {}
        for record in records:
            if record["id"] in deleted:
                continue
            statuses[record["vm_id"]] = TaskStatus(
                session_id=record["id"],
                vm_id=record["vm_id"],
                state=record["state"],
                is_running=False,
                action_count=record["action_count"],
                success_count=record["success_count"],
                failure_count=record["failure_count"],
                subtasks=record["subtasks"],
                current_subtask_index=record["current_subtask_index"],
                revision=record["revision"],
                session_order=record["session_order"],
            )
        for session in changes.values():
            if session is None or session.vm_id is None:
                continue
            status = _make_status(session)
            previous = statuses.get(session.vm_id)
            if previous is None or status.session_order >= previous.session_order:
                statuses[session.vm_id] = status
        return list(statuses.values())
    finally:
        _snapshot_readers.pop(token, None)


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
        session_order=getattr(session, "session_order", 0),
        manual_input=getattr(session, "manual_input", None),
    )


def _subtask_list(ctx) -> list[dict]:
    """WS配信用のサブタスク一覧。"""
    return [{"id": s.id, "description": s.description} for s in ctx.subtasks]
