"""タスク永続化 — `data/tasks/` 配下にJSON保存し、再起動後も履歴を復元する。

保存タイミングは TaskSession の状態変化・アクション実行ごと。
サーバー起動時は実行中だったレコードを `interrupted` に倒す。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from pathlib import Path

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
    }
)

INTERRUPTED = "interrupted"


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
    goal: dict = dataclasses.field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "instruction": self.instruction,
            "state": self.state,
            "success": self.success,
            "actions": [dataclasses.asdict(a) for a in self.actions],
            "subtasks": self.subtasks,
            "current_subtask_index": self.current_subtask_index,
            "vm_id": self.vm_id,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "llm_calls": self.llm_calls,
            "goal": self.goal,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "action_count": len(self.actions),
            "success_count": sum(1 for a in self.actions if a.success),
            "failure_count": sum(1 for a in self.actions if not a.success),
        }

    @staticmethod
    def from_dict(d: dict) -> TaskRecord:
        return TaskRecord(
            id=str(d.get("id", "")),
            instruction=str(d.get("instruction", "")),
            state=str(d.get("state", "idle")),
            success=d.get("success"),
            actions=[StoredAction(**a) for a in d.get("actions", [])],
            subtasks=list(d.get("subtasks", [])),
            current_subtask_index=int(d.get("current_subtask_index", 0)),
            vm_id=d.get("vm_id"),
            prompt_tokens=int(d.get("prompt_tokens", 0)),
            completion_tokens=int(d.get("completion_tokens", 0)),
            llm_calls=int(d.get("llm_calls", 0)),
            goal=dict(d.get("goal", {})),
            created_at=float(d.get("created_at", 0.0)),
            updated_at=float(d.get("updated_at", 0.0)),
        )


class TaskStore:
    """タスクレコードのファイル保存・読み出し。

    Args:
        root: 保存ディレクトリ。省略時は `$DATA_DIR/tasks`（既定 `./data/tasks`）。
    """

    def __init__(self, root: str | Path | None = None) -> None:
        base = root or os.environ.get("DATA_DIR", "./data")
        self._dir = Path(base) / "tasks"
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # 書き込めない環境（権限等）では一時ディレクトリに退避
            import tempfile

            self._dir = Path(tempfile.mkdtemp(prefix="ai-agent-tasks-"))
            logger.warning("タスク保存先に書き込めないため %s を使用", self._dir)

    def save(self, record: TaskRecord) -> None:
        """レコードを保存（upsert）。"""
        record.updated_at = time.time()
        if not record.created_at:
            record.created_at = record.updated_at
        path = self._dir / f"{record.id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record.to_dict(), ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def load(self, task_id: str) -> TaskRecord | None:
        """1件読み出し。なければ None。"""
        path = self._dir / f"{task_id}.json"
        if not path.is_file():
            return None
        try:
            return TaskRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.warning("タスク記録の読み出しに失敗 %s: %s", task_id, e)
            return None

    def list(self, limit: int = 50) -> list[TaskRecord]:
        """更新が新しい順に一覧。壊れたファイルは読み飛ばす。"""
        records: list[TaskRecord] = []
        for path in self._dir.glob("*.json"):
            rec = self.load(path.stem)
            if rec is not None:
                records.append(rec)
        records.sort(key=lambda r: r.updated_at, reverse=True)
        return records[:limit]

    def latest(self) -> TaskRecord | None:
        """最新の1件。なければ None。"""
        items = self.list(limit=1)
        return items[0] if items else None

    def delete(self, task_id: str) -> bool:
        """1件削除する。存在しなければ False。"""
        path = self._dir / f"{task_id}.json"
        if not path.is_file():
            return False
        try:
            path.unlink()
        except OSError as e:
            logger.warning("タスク記録の削除に失敗 %s: %s", task_id, e)
            return False
        return True

    def mark_interrupted(self) -> int:
        """実行中のまま残っているレコードを interrupted に倒す。

        Returns:
            更新した件数。
        """
        count = 0
        for rec in self.list(limit=1000):
            if rec.state in ACTIVE_STATES:
                rec.state = INTERRUPTED
                rec.success = False
                self.save(rec)
                count += 1
        if count:
            logger.info("中断タスクを %d 件マークしました", count)
        return count
