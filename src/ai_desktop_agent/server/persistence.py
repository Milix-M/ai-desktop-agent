"""Durable local JSON writes shared by history and VM allocation metadata."""

import fcntl
import json
import math
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import TextIO

from ai_desktop_agent.validation import validate_text


class StorageError(RuntimeError):
    """A durable write or a trustworthy metadata read could not complete."""


def unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate keys and values that cannot survive a durable JSON write."""
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("保存データのJSONに重複キーがあります")
        validate_text(key, "保存データのキー")
        _check_json_value(item)
        value[key] = item
    return value


def _check_json_value(value: object) -> None:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            validate_text(item, "保存データの値")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("保存データの数値は有限である必要があります")
        elif isinstance(item, list):
            pending.extend(item)
        # Nested objects have already passed this object_pairs_hook during decoding.


class DeletionError(StorageError):
    """Deletion failed, possibly after the entry disappeared from directory reads."""

    def __init__(self, path: Path, *, removed: bool) -> None:
        self.removed = removed
        reason = "削除結果を保存できません" if removed else "削除できません"
        super().__init__(f"{reason}: {path}。保存先を確認して再試行してください")


def durable_unlink(path: Path) -> bool:
    """Sync the parent even on a retry after an earlier unlink succeeded."""
    removed = False
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        raise DeletionError(path, removed=removed) from error
    return removed


def check_writable(directory: Path) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryFile(dir=directory) as probe:
            probe.write(b"probe")
            probe.flush()
            os.fsync(probe.fileno())
    except OSError as error:
        raise StorageError(
            f"保存先に書き込めません: {directory}。空き容量とアクセス権を確認してください"
        ) from error


def atomic_json(path: Path, value: object) -> None:
    _atomic_write(
        path, lambda output: json.dump(value, output, ensure_ascii=False, allow_nan=False)
    )


def atomic_text(path: Path, value: str) -> None:
    _atomic_write(path, lambda output: output.write(value))


def _atomic_write(path: Path, write: Callable[[TextIO], object]) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with ExitStack() as stack:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            stack.callback(os.close, descriptor)
            temporary = Path(name)
            output = stack.enter_context(
                os.fdopen(descriptor, "w", encoding="utf-8", closefd=False)
            )
            write(output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, TypeError, ValueError) as error:
        raise StorageError(
            f"保存に失敗しました: {path}。空き容量とアクセス権を確認してください"
        ) from error
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


@contextmanager
def metadata_lock(path: Path) -> Iterator[None]:
    with ExitStack() as stack:
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(
                path.with_suffix(path.suffix + ".lock"), os.O_CREAT | os.O_RDWR, 0o600
            )
            stack.callback(os.close, descriptor)
            lock = stack.enter_context(os.fdopen(descriptor, "a", closefd=False))
            fcntl.flock(lock, fcntl.LOCK_EX)
        except OSError as error:
            raise StorageError(f"保存先をロックできません: {path}") from error
        try:
            yield
        finally:
            with suppress(OSError):
                fcntl.flock(lock, fcntl.LOCK_UN)
