"""タスク永続化 — `data/tasks/` 配下にJSON保存し、再起動後も履歴を復元する。

保存タイミングは TaskSession の状態変化・アクション実行ごと。
サーバー起動時は実行中だったレコードを `interrupted` に倒す。
"""

from __future__ import annotations

import dataclasses
import fcntl
import heapq
import json
import logging
import math
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path

from ai_desktop_agent.agent.output import TaskOutput
from ai_desktop_agent.privacy import redact_data
from ai_desktop_agent.server.persistence import (
    StorageError,
    atomic_json,
    check_writable,
    durable_unlink,
    metadata_lock,
    unique_json_object,
)
from ai_desktop_agent.server.submissions import (
    SubmissionClaim,
    SubmissionConflictError,
    SubmissionGoneError,
    SubmissionPendingError,
    task_id_for,
)

logger = logging.getLogger(__name__)

# 実行中とみなす状態（これらで残っていたら起動時に interrupted へ）
ACTIVE_STATES = frozenset(
    {
        "understanding",
        "planning",
        "executing",
        "waiting",
        "verifying",
        "recovering",
        "paused",
        "awaiting_user",
    }
)

INTERRUPTED = "interrupted"
TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
MAX_JSON_DEPTH = 64
MAX_SESSION_ORDER = 2**53 - 1  # Exact integers in browser JSON consumers.


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("履歴には有限の数値が必要です")
    return number


def _reject_constant(value: str):
    raise ValueError(f"履歴の数値が不正です: {value}")


def _read_json(path: Path) -> dict:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        parse_float=_finite_float,
        parse_constant=_reject_constant,
        object_pairs_hook=unique_json_object,
    )
    _check_json_depth(value)
    return value


def _check_json_depth(value, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("履歴の入れ子が深すぎます")
    children = (
        value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    )
    for child in children:
        _check_json_depth(child, depth + 1)


def _validate_fields(data: dict, fields: dict[str, tuple[type, ...]]) -> None:
    for name, kinds in fields.items():
        if name in data and type(data[name]) not in kinds:
            raise ValueError(f"履歴の {name} の型が不正です")


def _validate_numbers(data: dict, names: tuple[str, ...], *, integer: bool = False) -> None:
    for name in names:
        value = data.get(name, 0)
        if type(value) not in ((int,) if integer else (int, float)):
            raise ValueError(f"履歴の {name} は数値である必要があります")
        try:
            finite = integer or math.isfinite(value)
        except OverflowError:
            finite = False
        if value < 0 or not finite:
            raise ValueError(f"履歴の {name} の値が不正です")


@dataclasses.dataclass
class StoredAction:
    """永続化用の1アクション記録。"""

    action_type: str
    params: dict
    description: str
    success: bool
    error_message: str = ""
    duration_ms: float = 0.0
    at: float = 0.0  # 記録時刻（epoch秒）
    reasoning: str = ""
    confidence: float = 1.0


@dataclasses.dataclass
class TaskRecord:
    """永続化用の1タスク記録。"""

    id: str
    instruction: str
    state: str
    success: bool | None = None
    actions: list[StoredAction] = dataclasses.field(default_factory=list)
    subtasks: list[dict] = dataclasses.field(default_factory=list)
    current_subtask_index: int = 0
    vm_id: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    decision_calls: int = 0  # 決定モデル（完了確認・回復方針）の呼出回数
    decision_escalations: int = 0  # 中間・失敗でLLMに退避した回数
    goal: dict = dataclasses.field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    revision: int = 0
    session_order: int = 0
    request_digest: str | None = None
    request_fingerprint: str | None = None
    execution_limits: dict = dataclasses.field(default_factory=dict)
    execution_usage: dict = dataclasses.field(default_factory=dict)
    stop_reason: str | None = None
    error_message: str = ""

    result: list[TaskOutput] = dataclasses.field(default_factory=list)

    def to_dict(self, *, include_actions: bool = True) -> dict:
        result = {
            "id": self.id,
            "instruction": self.instruction,
            "state": self.state,
            "success": self.success,
            "subtasks": self.subtasks,
            "current_subtask_index": self.current_subtask_index,
            "vm_id": self.vm_id,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "llm_calls": self.llm_calls,
            "decision_calls": self.decision_calls,
            "decision_escalations": self.decision_escalations,
            "goal": self.goal,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "revision": self.revision,
            "session_order": self.session_order,
            "request_digest": self.request_digest,
            "request_fingerprint": self.request_fingerprint,
            "execution_limits": self.execution_limits,
            "execution_usage": self.execution_usage,
            "stop_reason": self.stop_reason,
            "error_message": self.error_message,
            "action_count": len(self.actions),
            "success_count": sum(1 for a in self.actions if a.success),
            "failure_count": sum(1 for a in self.actions if not a.success),
        }
        if include_actions:
            result["result"] = [output.model_dump() for output in self.result]
            result["actions"] = [dataclasses.asdict(a) for a in self.actions]
        return result

    @staticmethod
    def from_file(path: Path) -> TaskRecord:
        record = TaskRecord.from_dict(redact_data(_read_json(path)))
        if record.id != path.stem:
            raise ValueError("タスクIDとファイル名が一致しません")
        return record

    @staticmethod
    def from_dict(d: dict) -> TaskRecord:
        if not isinstance(d, dict) or not TASK_ID.fullmatch(str(d.get("id", ""))):
            raise ValueError("タスクIDが不正です")
        _validate_fields(
            d,
            {
                "id": (str,),
                "instruction": (str,),
                "state": (str,),
                "success": (bool, type(None)),
                "vm_id": (str, type(None)),
                "stop_reason": (str, type(None)),
                "error_message": (str,),
                "request_digest": (str, type(None)),
                "request_fingerprint": (str, type(None)),
                "actions": (list,),
                "result": (list,),
                "subtasks": (list,),
                "goal": (dict,),
                "execution_limits": (dict,),
                "execution_usage": (dict,),
            },
        )
        _validate_numbers(d, ("created_at", "updated_at"))
        _validate_numbers(
            d,
            (
                "current_subtask_index",
                "prompt_tokens",
                "completion_tokens",
                "llm_calls",
                "decision_calls",
                "decision_escalations",
                "revision",
                "session_order",
            ),
            integer=True,
        )
        if d.get("session_order", 0) > MAX_SESSION_ORDER:
            raise ValueError("タスクの開始順が範囲外です")
        digest, request_fp = d.get("request_digest"), d.get("request_fingerprint")
        if digest is not None or request_fp is not None:
            if not all(
                isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)
                for v in (digest, request_fp)
            ):
                raise ValueError("受付記録の識別子が不正です")
            if d["id"] != task_id_for(digest):
                raise ValueError("受付記録とタスクIDが一致しません")
        if any(not isinstance(subtask, dict) for subtask in d.get("subtasks", [])):
            raise ValueError("サブタスクの型が不正です")
        for action in d.get("actions", []):
            if not isinstance(action, dict):
                raise ValueError("操作履歴の型が不正です")
            _validate_fields(
                action,
                {
                    "action_type": (str,),
                    "params": (dict,),
                    "description": (str,),
                    "success": (bool,),
                    "error_message": (str,),
                    "reasoning": (str,),
                },
            )
            _validate_numbers(action, ("duration_ms", "at", "confidence"))
        return TaskRecord(
            id=str(d.get("id", "")),
            instruction=str(d.get("instruction", "")),
            state=str(d.get("state", "idle")),
            success=d.get("success"),
            result=[TaskOutput.model_validate(item) for item in d.get("result", [])],
            actions=[StoredAction(**a) for a in d.get("actions", [])],
            subtasks=list(d.get("subtasks", [])),
            current_subtask_index=int(d.get("current_subtask_index", 0)),
            vm_id=d.get("vm_id"),
            prompt_tokens=int(d.get("prompt_tokens", 0)),
            completion_tokens=int(d.get("completion_tokens", 0)),
            llm_calls=int(d.get("llm_calls", 0)),
            decision_calls=int(d.get("decision_calls", 0)),
            decision_escalations=int(d.get("decision_escalations", 0)),
            goal=dict(d.get("goal", {})),
            created_at=float(d.get("created_at", 0.0)),
            updated_at=float(d.get("updated_at", 0.0)),
            revision=int(d.get("revision", 0)),
            session_order=d.get("session_order", 0),
            request_digest=digest,
            request_fingerprint=request_fp,
            execution_limits=dict(d.get("execution_limits", {})),
            execution_usage=dict(d.get("execution_usage", {})),
            stop_reason=d.get("stop_reason"),
            error_message=str(d.get("error_message", "")),
        )


class TaskStore:
    """タスクレコードのファイル保存・読み出し。

    Args:
        root: 保存ディレクトリ。省略時は `$DATA_DIR/tasks`（既定 `./data/tasks`）。
    """

    def __init__(self, root: str | Path | None = None) -> None:
        base = root or os.environ.get("DATA_DIR", "./data")
        self._dir = Path(base) / "tasks"
        self._order_file = self._dir.parent / "task-order.json"
        self._submissions_dir = self._dir.parent / "submissions"
        self._order_checked = False
        self._last_order = 0
        self.check_writable()

    def check_writable(self) -> None:
        check_writable(self._dir)

    def reserve_session_order(self) -> int:
        """Allocate a durable order before the first task state is published."""
        with metadata_lock(self._order_file):
            previous = self._read_session_order()
            if not self._order_checked or previous < self._last_order:
                previous = max(previous, max((r.session_order for r in self._records()), default=0))
            previous = max(previous, self._last_order)
            if previous >= MAX_SESSION_ORDER:
                raise StorageError("タスクの開始順が上限に到達しました")
            order = previous + 1
            atomic_json(self._order_file, {"last_order": order})
            self._order_checked = True
            self._last_order = order
            return order

    def current_session_order(self) -> int:
        """Read a snapshot fence without allocating another task order."""
        with metadata_lock(self._order_file):
            return max(self._last_order, self._read_session_order())

    def _read_session_order(self) -> int:
        try:
            previous = _read_json(self._order_file)["last_order"]
            if type(previous) is not int or not 0 <= previous <= MAX_SESSION_ORDER:
                raise ValueError("開始順の記録が不正です")
            return previous
        except FileNotFoundError:
            return 0
        except (ValueError, TypeError, KeyError, OSError, RecursionError) as error:
            raise StorageError("タスクの開始順を読み出せません") from error

    def save(self, record: TaskRecord) -> None:
        """レコードを保存（upsert）。"""
        if not TASK_ID.fullmatch(record.id):
            raise ValueError("タスクIDが不正です")
        receipt = None
        if record.request_digest is not None:
            TaskRecord.from_dict(record.to_dict(include_actions=False))
            receipt = self._read_receipt(record.request_digest)
            if receipt and receipt["request_fingerprint"] != record.request_fingerprint:
                raise StorageError("受付記録とタスクの内容が一致しません")
        record.updated_at = time.time()
        if not record.created_at:
            record.created_at = record.updated_at
        path = self._dir / f"{record.id}.json"
        atomic_json(path, redact_data(record.to_dict()))
        if record.request_digest is not None and receipt is None:
            # No model call may start until both the task and this receipt are durable.
            self._write_receipt(record)

    def _receipt_path(self, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("受付記録の識別子が不正です")
        return self._submissions_dir / f"{digest}.json"

    def _read_receipt(self, digest: str) -> dict | None:
        try:
            value = _read_json(self._receipt_path(digest))
            if (
                not isinstance(value, dict)
                or value.get("task_id") != task_id_for(digest)
                or value.get("request_digest") != digest
                or not isinstance(value.get("request_fingerprint"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["request_fingerprint"])
            ):
                raise ValueError("受付記録が不正です")
            return value
        except FileNotFoundError:
            return None
        except (ValueError, TypeError, OSError, RecursionError) as error:
            raise StorageError("受付記録を読み出せません。保存先を確認してください") from error

    def _write_receipt(self, record: TaskRecord) -> None:
        atomic_json(
            self._receipt_path(record.request_digest),
            {
                "task_id": record.id,
                "request_digest": record.request_digest,
                "request_fingerprint": record.request_fingerprint,
            },
        )

    def claim_submission(self, digest: str, request_fp: str) -> SubmissionClaim:
        """Claim a retry key until the initial task record and receipt are committed."""
        path = self._receipt_path(digest)
        lock = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = os.fdopen(
                os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600), "a+b"
            )
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise SubmissionPendingError(
                    "同じ投入を受付中です。同じ内容で再送してください"
                ) from error
            receipt = self._read_receipt(digest)
            if receipt and receipt["request_fingerprint"] != request_fp:
                raise SubmissionConflictError("同じ投入キーを別の内容には使えません")
            task_id = task_id_for(digest)
            record = self.load(task_id)
            if record is None and (self._dir / f"{task_id}.json").exists():
                raise StorageError("受付済みタスクの履歴を読み出せません")
            if record is None and receipt is not None:
                raise SubmissionGoneError("この投入は受付済みで、履歴は削除されています")
            if record is not None:
                if record.request_digest != digest or record.request_fingerprint != request_fp:
                    if receipt:
                        raise StorageError("受付記録とタスクの内容が一致しません")
                    raise SubmissionConflictError("同じ投入キーを別の内容には使えません")
                if receipt is None:
                    self._write_receipt(record)
            return SubmissionClaim(digest, request_fp, task_id, lock, record=record)
        except BaseException as error:
            if lock is not None:
                lock.close()
            if isinstance(error, OSError):
                raise StorageError("受付記録にアクセスできません") from error
            raise

    def load(self, task_id: str) -> TaskRecord | None:
        """1件読み出し。なければ None。"""
        if not TASK_ID.fullmatch(task_id):
            return None
        path = self._dir / f"{task_id}.json"
        if not path.is_file():
            return None
        try:
            return TaskRecord.from_file(path)
        except (ValueError, TypeError, KeyError, AttributeError, OSError, RecursionError) as e:
            logger.warning("タスク記録の読み出しに失敗 %s: %s", task_id, e)
            return None

    def _records(self) -> Iterator[TaskRecord]:
        for path in self._dir.glob("*.json"):
            rec = self.load(path.stem)
            if rec is not None:
                yield rec

    def list(self, limit: int | None = 50) -> list[TaskRecord]:
        """開始が新しい順に一覧。旧履歴は更新時刻で比較し、壊れたファイルは読み飛ばす。"""
        if limit is None:
            return sorted(
                self._records(), key=lambda r: (r.session_order, r.updated_at), reverse=True
            )
        return heapq.nlargest(limit, self._records(), key=lambda r: (r.session_order, r.updated_at))

    def summaries(self, limit: int = 50) -> list[dict]:
        """Keep only summaries for the requested page, without retaining action histories."""
        summaries = (record.to_dict(include_actions=False) for record in self._records())
        return heapq.nlargest(limit, summaries, key=lambda d: (d["session_order"], d["updated_at"]))

    def latest_per_vm(self) -> list[dict]:
        latest: dict[str, dict] = {}
        for record in self._records():
            if record.vm_id is None:
                continue
            previous = latest.get(record.vm_id)
            if previous is None or (record.session_order, record.updated_at) > (
                previous["session_order"],
                previous["updated_at"],
            ):
                latest[record.vm_id] = record.to_dict(include_actions=False)
        return sorted(
            latest.values(), key=lambda d: (d["session_order"], d["updated_at"]), reverse=True
        )

    def latest(self) -> TaskRecord | None:
        """最新の1件。なければ None。"""
        items = self.list(limit=1)
        return items[0] if items else None

    def delete(self, task_id: str) -> bool:
        """1件削除する。存在しなければ False。"""
        if not TASK_ID.fullmatch(task_id):
            return False
        path = self._dir / f"{task_id}.json"
        return durable_unlink(path)

    def mark_interrupted(self) -> int:
        """実行中のまま残っているレコードを interrupted に倒す。

        Returns:
            更新した件数。
        """
        count = 0
        for rec in self._records():
            if rec.state in ACTIVE_STATES:
                rec.state = INTERRUPTED
                rec.success = False
                rec.revision += 1
                self.save(rec)
                count += 1
        if count:
            logger.info("中断タスクを %d 件マークしました", count)
        return count

    def redact_existing(self) -> int:
        """Rewrite only valid records whose configured credentials need redaction."""
        count = 0
        for path in self._dir.glob("*.json"):
            if not TASK_ID.fullmatch(path.stem):
                continue
            try:
                original = _read_json(path)
                record = TaskRecord.from_dict(original)
                if record.id != path.stem:
                    continue
                cleaned = redact_data(original)
            except (ValueError, TypeError, KeyError, AttributeError, OSError, RecursionError):
                continue
            if cleaned != original:
                atomic_json(path, cleaned)
                count += 1
        return count
