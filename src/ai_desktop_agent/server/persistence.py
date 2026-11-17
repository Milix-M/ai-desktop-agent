"""Durable local JSON writes shared by history and VM allocation metadata."""

import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TextIO


class StorageError(RuntimeError):
    """A durable write or a trustworthy metadata read could not complete."""


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
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
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
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path.with_suffix(path.suffix + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as error:
        raise StorageError(f"保存先をロックできません: {path}") from error
    with os.fdopen(descriptor, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
        except OSError as error:
            raise StorageError(f"保存先をロックできません: {path}") from error
        try:
            yield
        finally:
            with suppress(OSError):
                fcntl.flock(lock, fcntl.LOCK_UN)
