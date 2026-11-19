"""Durable, retryable acceptance of VM creation without replaying uncertain side effects."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import BinaryIO, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, model_validator

from ai_desktop_agent.server.kvm import KvmUnavailableError
from ai_desktop_agent.server.persistence import (
    StorageError,
    atomic_json,
    metadata_lock,
    unique_json_object,
)
from ai_desktop_agent.server.vm_control import DockerUnavailableError
from ai_desktop_agent.server.vm_pool import ID_PREFIX, VmInfo, VmPool
from ai_desktop_agent.validation import validate_text
from ai_desktop_agent.vm.async_io import (
    MAX_PENDING_VM_CREATIONS,
    VmCreationBusyError,
    VmCreationSlot,
    reserve_vm_creation_slot,
    run_display_call,
    run_reserved_vm_creation,
    run_vm_call,
)

logger = logging.getLogger(__name__)
_JOB_ID = re.compile(r"creation-[0-9a-f]{64}\Z")
_KEY = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")


class CreationConflictError(ValueError):
    pass


def _fingerprint(name: str | None, kind: str) -> str:
    payload = json.dumps({"name": name, "kind": kind}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


class CreationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)

    version: Literal[1] = 1
    id: str = Field(pattern=r"^creation-[0-9a-f]{64}$")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    vm_id: str
    name: str | None = Field(max_length=128)
    kind: Literal["qemu", "container", "android"]
    state: Literal["queued", "running", "succeeded", "failed"]
    created_at: float = Field(ge=0)
    updated_at: float = Field(ge=0)
    result: VmInfo | None = None
    error: str | None = Field(default=None, max_length=2048)
    error_status: Literal[400, 409, 503] | None = None

    @model_validator(mode="before")
    @classmethod
    def version_is_an_integer(cls, value, info: ValidationInfo):
        if (
            isinstance(value, dict)
            and ("version" in value or info.mode == "json")
            and type(value.get("version")) is not int
        ):
            raise ValueError("receipt version must be an integer")
        return value

    @model_validator(mode="after")
    def consistent(self) -> Self:
        digest = self.id.removeprefix("creation-")
        if self.vm_id != f"{ID_PREFIX[self.kind]}-{digest[:24]}":
            raise ValueError("creation target does not match its receipt")
        if self.fingerprint != _fingerprint(self.name, self.kind):
            raise ValueError("creation payload does not match its fingerprint")
        if self.updated_at < self.created_at:
            raise ValueError("creation timestamps are out of order")
        if self.state == "succeeded":
            if (
                self.result is None
                or self.result.id != self.vm_id
                or self.result.kind != self.kind
                or self.result.managed is not True
                or self.error is not None
                or self.error_status is not None
            ):
                raise ValueError("invalid creation result")
        elif self.result is not None:
            raise ValueError("unfinished creation has a result")
        if self.state == "failed":
            if not self.error or self.error_status is None:
                raise ValueError("failed creation has no failure")
        elif self.error is not None or self.error_status is not None:
            raise ValueError("non-failed creation has a failure")
        return self

    def public(self) -> dict:
        return self.model_dump(mode="json", exclude={"fingerprint", "version"})

    def updated(self, **changes) -> CreationRecord:
        return self.model_copy(update={**changes, "updated_at": max(time.time(), self.updated_at)})


class CreationStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def acquire_owner(self) -> BinaryIO:
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self.directory / "owner.lock", os.O_CREAT | os.O_RDWR, 0o600)
            owner = os.fdopen(descriptor, "a+b")
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                owner.close()
                raise DockerUnavailableError(
                    "同じ保存先を使う別のバックエンドが稼働しています"
                ) from error
            except OSError:
                owner.close()
                raise
            return owner
        except DockerUnavailableError:
            raise
        except OSError as error:
            raise StorageError("環境作成の保存先を利用できません") from error

    def _path(self, job_id: str) -> Path:
        if not _JOB_ID.fullmatch(job_id):
            raise ValueError("作成要求のIDが不正です")
        return self.directory / (job_id + ".json")

    def read(self, job_id: str) -> CreationRecord | None:
        path = self._path(job_id)
        try:
            if path.is_symlink():
                raise ValueError("receipt must not be a symlink")
            with path.open("rb") as source:
                payload = source.read(64 * 1024 + 1)
            if len(payload) > 64 * 1024:
                raise ValueError("receipt is too large")
            record = CreationRecord.model_validate_json(payload)
            # Keep Pydantic's strict JSON schema and recursion bound, then reject
            # repeated fields that its JSON decoder otherwise resolves by order.
            json.loads(payload, object_pairs_hook=unique_json_object)
            if record.id != job_id:
                raise ValueError("receipt filename mismatch")
            return record
        except FileNotFoundError:
            return None
        except (OSError, ValueError, ValidationError) as error:
            raise StorageError(
                "環境作成の記録を読み込めません。保存先とバックアップを確認してください"
            ) from error

    def write(self, record: CreationRecord) -> None:
        try:
            # Validate internal updates too, before publishing a durable transition.
            record = CreationRecord.model_validate_json(record.model_dump_json())
        except ValidationError as error:
            raise StorageError("環境作成の記録が不正です") from error
        path = self._path(record.id)
        with metadata_lock(path):
            atomic_json(path, record.model_dump(mode="json"))

    def incomplete(self) -> list[CreationRecord]:
        pending = []
        for path in sorted(self.directory.glob("creation-*.json")):
            try:
                record = self.read(path.stem)
            except ValueError as error:
                raise StorageError("環境作成の記録の名前が不正です") from error
            if record is not None and record.state in {"queued", "running"}:
                pending.append(record)
        return pending


class VmCreationService:
    def __init__(self, pool: VmPool, store: CreationStore):
        self.pool, self.store = pool, store
        self._ownership: asyncio.Task | None = None
        self._starting: asyncio.Task | None = None
        self._worker: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None
        self._owner: BinaryIO | None = None
        self._closing = False
        self._poison: StorageError | None = None
        self._submit_lock = asyncio.Lock()
        self._submissions: set[asyncio.Task] = set()
        self._pending: dict[str, asyncio.Event] = {}
        self._slots: dict[str, VmCreationSlot] = {}
        self._queue: asyncio.Queue[CreationRecord | None] = asyncio.Queue()

    async def acquire_ownership(self) -> None:
        """Claim the backend's existing lease before recovery or any other startup writes."""
        self._check_available()
        if (
            self._ownership is not None
            and self._ownership.done()
            and not self._ownership.cancelled()
            and self._ownership.exception() is not None
        ):
            self._ownership = None
        if self._ownership is None:

            def acquire():
                # Retain the descriptor even if the awaiting lifespan is cancelled.
                self._owner = self.store.acquire_owner()

            self._ownership = asyncio.create_task(run_display_call(acquire))
            self._ownership.add_done_callback(self._retrieve_error)
        await asyncio.shield(self._ownership)
        self._check_available()

    async def start(self) -> None:
        if self._closing:
            raise DockerUnavailableError("バックエンドを終了しています")
        if (
            self._starting is not None
            and self._starting.done()
            and not self._starting.cancelled()
            and isinstance(
                self._starting.exception(), (DockerUnavailableError, VmCreationBusyError)
            )
        ):
            # A daemon outage during recovery can be retried without starting new work.
            self._starting = None
        if self._starting is None:
            self._starting = asyncio.create_task(self._start())
            self._starting.add_done_callback(self._retrieve_error)
        await asyncio.shield(self._starting)
        self._check_available()

    @staticmethod
    def _retrieve_error(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()

    def _check_available(self) -> None:
        if self._closing:
            raise DockerUnavailableError("バックエンドを終了しています")
        if self._poison is not None:
            raise self._poison

    async def _start(self) -> None:
        await self.acquire_ownership()
        try:
            pending = await run_display_call(self.store.incomplete)
            queued = []
            for record in pending:
                if record.state == "queued":
                    if len(queued) >= MAX_PENDING_VM_CREATIONS:
                        raise StorageError("保存された作成待ちの件数が上限を超えています")
                    queued.append(record)
                    continue
                existing = await run_vm_call(self.pool.get_vm, record.vm_id)
                if existing is not None:
                    recovered = record.updated(state="succeeded", result=existing)
                else:
                    await run_vm_call(self.pool.remove_vm, record.vm_id)
                    recovered = self._failed(
                        record,
                        "バックエンドの再起動で作成が中断しました。環境の状態を確認してください",
                        503,
                    )
                await run_display_call(self.store.write, recovered)
            for record in queued:
                self._enqueue(record, reserve_vm_creation_slot())
            self._worker = asyncio.create_task(self._work())
        except BaseException:
            for slot in self._slots.values():
                slot.release()
            self._slots.clear()
            self._pending.clear()
            while not self._queue.empty():
                self._queue.get_nowait()
            # The lifespan may still be draining startup/shutdown work. Only close()
            # releases its lease; a recoverable daemon failure can retry under this owner.
            raise

    def _enqueue(self, record: CreationRecord, slot: VmCreationSlot) -> None:
        self._pending[record.id] = asyncio.Event()
        self._slots[record.id] = slot
        self._queue.put_nowait(record)

    async def submit(self, key: str, name: str | None, kind: str) -> CreationRecord:
        # The service owns acceptance through publication even if the HTTP caller disconnects.
        task = asyncio.create_task(self._submit(key, name, kind))
        self._submissions.add(task)

        def done(finished):
            self._submissions.discard(finished)
            self._retrieve_error(finished)

        task.add_done_callback(done)
        return await asyncio.shield(task)

    async def _submit(self, key: str, name: str | None, kind: str) -> CreationRecord:
        if name is not None:
            validate_text(name, "環境名")
        if (
            not _KEY.fullmatch(key)
            or kind not in ID_PREFIX
            or (name is not None and len(name) > 128)
        ):
            raise ValueError("作成要求が不正です")
        name = name or None
        await self.start()
        async with self._submit_lock:
            self._check_available()
            digest = hashlib.sha256(key.encode()).hexdigest()
            job_id = "creation-" + digest
            fingerprint = _fingerprint(name, kind)
            existing = await run_display_call(self.store.read, job_id)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise CreationConflictError("同じ受付キーで作成内容を変更できません")
                # A failed directory fsync may have published a queued receipt before the
                # caller received an error. It is safe to schedule it under this owner.
                if existing.state == "queued" and existing.id not in self._pending:
                    self._check_capacity()
                    self._enqueue(existing, reserve_vm_creation_slot())
                return existing
            self._check_capacity()
            now = time.time()
            record = CreationRecord(
                id=job_id,
                fingerprint=fingerprint,
                vm_id=f"{ID_PREFIX[kind]}-{digest[:24]}",
                name=name,
                kind=kind,
                state="queued",
                created_at=now,
                updated_at=now,
            )
            slot = reserve_vm_creation_slot()
            try:
                await run_display_call(self.store.write, record)
                self._enqueue(record, slot)
            except BaseException:
                slot.release()
                raise
            return record

    def _check_capacity(self) -> None:
        if len(self._pending) >= MAX_PENDING_VM_CREATIONS:
            raise VmCreationBusyError(
                "環境の作成要求が混み合っています。しばらく待って再操作してください"
            )

    async def get(self, job_id: str) -> CreationRecord | None:
        await self.start()
        return await run_display_call(self.store.read, job_id)

    async def wait(self, job_id: str) -> CreationRecord | None:
        await self.start()
        event = self._pending.get(job_id)
        if event is not None:
            await event.wait()
        return await self.get(job_id)

    @staticmethod
    def _failed(record: CreationRecord, message: str, status: int) -> CreationRecord:
        message = message.encode("utf-8", errors="replace").decode("utf-8")
        message = (
            message[:2048]
            if message.strip()
            else "環境を作成できません。環境の状態を確認してください"
        )
        return record.updated(state="failed", error=message, error_status=status)

    async def _one(self, record: CreationRecord) -> None:
        running = record.updated(state="running")
        await run_display_call(self.store.write, running)
        try:
            if self._closing:
                raise DockerUnavailableError("作成の開始前にバックエンドを終了しました")
            result = await run_reserved_vm_creation(
                self._slots[record.id],
                self.pool.create_vm,
                name=record.name,
                kind=record.kind,
                vm_id=record.vm_id,
            )
            finished = running.updated(state="succeeded", result=result)
        except KvmUnavailableError as error:
            finished = self._failed(running, str(error), 409)
        except DockerUnavailableError as error:
            finished = self._failed(running, str(error), 503)
        except ValueError as error:
            finished = self._failed(running, str(error), 400)
        except StorageError:
            raise
        except Exception:
            logger.exception("環境の作成に失敗しました: %s", record.id)
            finished = self._failed(
                running, "環境を作成できません。環境の状態を確認してください", 503
            )
        # An admission may be returning an older queued snapshot from its I/O thread.
        # Publish completion under the same lock before removing the pending identity.
        async with self._submit_lock:
            await run_display_call(self.store.write, finished)

    async def _work(self) -> None:
        while True:
            record = await self._queue.get()
            if record is None:
                return
            try:
                if self._closing:
                    await run_display_call(
                        self.store.write,
                        self._failed(record, "作成の開始前にバックエンドを終了しました", 503),
                    )
                elif self._poison is None:
                    await self._one(record)
            except StorageError as error:
                self._poison = error
                logger.exception("環境作成の記録を保存できません")
            finally:
                self._pending.pop(record.id).set()
                self._slots.pop(record.id).release()

    async def close(self) -> None:
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
            self._close_task.add_done_callback(self._retrieve_error)
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        if self._submissions:
            await asyncio.gather(*self._submissions, return_exceptions=True)
        if self._ownership is not None:
            await asyncio.gather(self._ownership, return_exceptions=True)
        if self._starting is not None:
            await asyncio.gather(self._starting, return_exceptions=True)
        if self._worker is not None:
            while not self._queue.empty():
                record = self._queue.get_nowait()
                if record is not None:
                    try:
                        await run_display_call(
                            self.store.write,
                            self._failed(record, "作成の開始前にバックエンドを終了しました", 503),
                        )
                    except StorageError:
                        logger.exception("終了時に環境作成の記録を保存できません")
                    self._pending.pop(record.id).set()
                    self._slots.pop(record.id).release()
            self._queue.put_nowait(None)
            await asyncio.shield(self._worker)
            self._worker = None
        if self._owner is not None:
            self._owner.close()
            self._owner = None
