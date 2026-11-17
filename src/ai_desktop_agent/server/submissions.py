"""Retry identities and ownership of an in-progress task submission."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from ai_desktop_agent.server.session import TaskSession
    from ai_desktop_agent.server.store import TaskRecord


class SubmissionConflictError(RuntimeError):
    pass


class SubmissionGoneError(RuntimeError):
    pass


class SubmissionPendingError(RuntimeError):
    pass


def key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def task_id_for(digest: str) -> str:
    return "req-" + digest[:32]


def fingerprint(payload: dict) -> str:
    value = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class SubmissionClaim:
    digest: str
    fingerprint: str
    task_id: str
    lock: BinaryIO
    record: TaskRecord | None = None
    session: TaskSession | None = None

    def close(self) -> None:
        # Closing this descriptor also releases flock, including after a process crash.
        self.lock.close()
