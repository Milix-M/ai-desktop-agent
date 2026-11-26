"""Persistent task schedules owned by the active backend process."""

import asyncio
import logging
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field, StringConstraints, model_validator

from ai_desktop_agent.privacy import redact
from ai_desktop_agent.server.submissions import key_digest
from ai_desktop_agent.server.vm_pool import ID_PREFIX
from ai_desktop_agent.server.workspaces import IDENTIFIER_PATTERN
from ai_desktop_agent.vm.async_io import run_display_call

logger = logging.getLogger(__name__)


class ScheduleRequest(BaseModel):
    instruction: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)
    ]
    vm_id: Annotated[str, StringConstraints(pattern=rf"^{IDENTIFIER_PATTERN}$")] | None = None
    vm_kind: Literal["qemu", "container", "android"] | None = None
    run_at: int = Field(gt=0, le=253402300799)
    interval: Literal[0, 86400, 604800] = 0

    @model_validator(mode="after")
    def exactly_one_target(self):
        if (self.vm_kind is None) == (self.vm_id is None):
            raise ValueError("既存環境または使い捨て環境のタイプを指定してください")
        return self


class CreateSchedule(ScheduleRequest):
    request_id: UUID | None = None


class Schedule(ScheduleRequest):
    id: str
    next_run: int | None
    preparing: bool = False
    last_task_id: str | None = None
    last_error: str | None = None


class EnvironmentStartingError(Exception):
    """Leave this occurrence pending while its environment boots."""


class DisposableRun(BaseModel):
    token: str
    vm_id: str
    schedule_id: str
    due: int
    instance_id: str | None

    @property
    def creation_key(self):
        return "environment-" + self.token

    @property
    def task_key(self):
        return "task-" + self.token


class Scheduler:
    def __init__(
        self,
        submit: Callable[[Schedule, str], Awaitable[str]],
        root: Path | None = None,
        *,
        retire: Callable[[DisposableRun], Awaitable[bool]] | None = None,
    ):
        self.retire = retire
        self.submit = submit
        self.path = (root or Path(os.environ.get("DATA_DIR", "./data"))) / "schedules" / "tasks.db"
        self.dispatch_lock = asyncio.Lock()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            columns = {r[1] for r in db.execute("PRAGMA table_info(schedules)")}
            legacy = bool(columns) and "vm_kind" not in columns
            if legacy:
                db.execute("ALTER TABLE schedules RENAME TO fixed_schedules")
            db.execute("""CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY, instruction TEXT NOT NULL, vm_id TEXT, vm_kind TEXT,
                run_at INTEGER NOT NULL, interval INTEGER NOT NULL, next_run INTEGER,
                last_task_id TEXT, last_error TEXT,
                CHECK ((vm_id IS NULL) != (vm_kind IS NULL)))""")
            if legacy:
                fields = "id,instruction,vm_id,run_at,interval,next_run,last_task_id,last_error"
                db.execute(f"INSERT INTO schedules ({fields}) SELECT {fields} FROM fixed_schedules")
                db.execute("DROP TABLE fixed_schedules")
            db.execute("CREATE INDEX IF NOT EXISTS schedules_due ON schedules(next_run,id)")
            db.execute("""CREATE TABLE IF NOT EXISTS disposable_runs (
                token TEXT PRIMARY KEY, vm_id TEXT UNIQUE NOT NULL, schedule_id TEXT NOT NULL,
                due INTEGER NOT NULL, instance_id TEXT, UNIQUE(schedule_id,due))""")

    async def reserve_environment(self, item: Schedule) -> DisposableRun:
        # Persist ownership before creation can reach Docker. Retries reuse this private key.
        token = uuid4().hex
        digest = key_digest("environment-" + token)
        vm_id = f"{ID_PREFIX[item.vm_kind]}-{digest[:24]}"
        rows = await run_display_call(
            self._query,
            """
            INSERT INTO disposable_runs (token,vm_id,schedule_id,due) VALUES (?,?,?,?)
            ON CONFLICT(schedule_id,due) DO UPDATE SET due=excluded.due RETURNING *""",
            (token, vm_id, item.id, item.next_run),
        )
        return DisposableRun(**rows[0])

    async def environment_owner(self, vm_id: str) -> DisposableRun | None:
        rows = await run_display_call(
            self._query, "SELECT * FROM disposable_runs WHERE vm_id=?", (vm_id,)
        )
        return DisposableRun(**rows[0]) if rows else None

    async def bind_instance(self, run: DisposableRun, instance_id: str):
        if run.instance_id is None:
            rows = await run_display_call(
                self._query,
                """UPDATE disposable_runs SET instance_id=? WHERE token=?
                   AND (instance_id IS NULL OR instance_id=?) RETURNING token""",
                (instance_id, run.token, instance_id),
            )
            if not rows:
                raise HTTPException(409, "予約した環境が置き換えられました")
            run.instance_id = instance_id
        elif run.instance_id != instance_id:
            raise HTTPException(409, "予約した環境が置き換えられました")

    async def retire_finished(self):
        if self.retire is None:
            return
        rows = await run_display_call(
            self._query,
            """SELECT r.* FROM disposable_runs r
            WHERE NOT EXISTS (SELECT 1 FROM schedules s WHERE s.id=r.schedule_id
                              AND s.next_run=r.due AND s.vm_kind IS NOT NULL)""",
        )
        for row in rows:
            run = DisposableRun(**row)
            try:
                if await self.retire(run):
                    await run_display_call(
                        self._query, "DELETE FROM disposable_runs WHERE token=?", (run.token,)
                    )
            except Exception:
                logger.exception("使い捨て環境の削除を再試行します: %s", run.vm_id)

    def _query(self, sql, params=()):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchall()

    def _insert(self, item: Schedule):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM schedules WHERE id=?", (item.id,)).fetchone()
            if row:
                existing = Schedule(**row)
                if any(
                    getattr(existing, k) != getattr(item, k) for k in ScheduleRequest.model_fields
                ):
                    raise HTTPException(409, "この予約IDは別の予約で使用されています")
                return existing
            if item.run_at <= time.time():
                raise HTTPException(422, "未来の日時を指定してください")
            conn.execute(
                """INSERT INTO schedules (id,instruction,vm_id,vm_kind,run_at,interval,next_run)
                   VALUES (:id,:instruction,:vm_id,:vm_kind,:run_at,:interval,:next_run)""",
                item.model_dump(),
            )
            return item

    async def add(self, req: CreateSchedule):
        if redact(req.instruction) != req.instruction:
            raise HTTPException(422, "予約する指示から認証情報を取り除いてください")
        item = Schedule(
            **req.model_dump(exclude={"request_id"}),
            id=(req.request_id or uuid4()).hex,
            next_run=req.run_at,
        )
        return await run_display_call(self._insert, item)

    async def list_all(self):
        rows = await run_display_call(
            self._query,
            """SELECT s.*, EXISTS (
                SELECT 1 FROM disposable_runs r WHERE r.schedule_id=s.id AND r.due=s.next_run
            ) AS preparing FROM schedules s ORDER BY next_run IS NULL, next_run, run_at DESC""",
        )
        return [Schedule(**row) for row in rows]

    async def remove(self, schedule_id: str):
        async with self.dispatch_lock:
            await run_display_call(self._query, "DELETE FROM schedules WHERE id=?", (schedule_id,))

    async def dispatch_due(self):
        rows = await run_display_call(
            self._query,
            "SELECT id FROM schedules WHERE next_run<=? ORDER BY next_run,id",
            (int(time.time()),),
        )
        for row in rows:
            async with self.dispatch_lock:
                current = await run_display_call(
                    self._query,
                    "SELECT * FROM schedules WHERE id=? AND next_run<=?",
                    (row["id"], int(time.time())),
                )
                if not current:
                    continue
                item = Schedule(**current[0])
                due = item.next_run
                assert due is not None
                task_id = error = None
                try:
                    task_id = await self.submit(item, f"schedule-{item.id}-{due}")
                except EnvironmentStartingError:
                    continue
                except HTTPException as exc:
                    error = redact(str(exc.detail))
                except Exception:
                    logger.exception("予約タスクを開始できませんでした")
                    error = "タスクを開始できませんでした"
                next_run = None
                if item.interval:
                    elapsed = max(0, int(time.time()) - due)
                    next_run = due + (elapsed // item.interval + 1) * item.interval
                await run_display_call(
                    self._query,
                    """UPDATE schedules SET next_run=?,last_task_id=?,last_error=?
                        WHERE id=?""",
                    (next_run, task_id, error, item.id),
                )

    async def run(self):
        while True:
            try:
                await self.retire_finished()
                await self.dispatch_due()
            except Exception:
                logger.exception("スケジュールを処理できませんでした")
            await asyncio.sleep(1)
