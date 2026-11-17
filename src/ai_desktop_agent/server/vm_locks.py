"""Serialize allocations and each environment's mutations within one backend process."""

import asyncio
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from functools import wraps

_allocations = threading.RLock()
_registry_lock = threading.Lock()


class _Entry:
    def __init__(self):
        self.lock = threading.RLock()
        self.users = 0


_environments: dict[str, _Entry] = {}


class AsyncKeyedLocks:
    """Hold a request's handover through cancellation cleanup, independently per key.

    Used only by the server event loop. Synchronous Docker workers use environment_lock.
    """

    def __init__(self):
        self._entries: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, key: str):
        lock, users = self._entries.get(key, (asyncio.Lock(), 0))
        self._entries[key] = lock, users + 1
        try:
            async with lock:
                yield
        finally:
            _, users = self._entries[key]
            if users == 1:
                del self._entries[key]
            else:
                self._entries[key] = lock, users - 1

    def locked(self, key: str) -> bool:
        entry = self._entries.get(key)
        return entry is not None and entry[0].locked()


@contextmanager
def environment_lock(vm_id: str) -> Iterator[None]:
    # Count waiting users as well as holders, so an entry cannot be replaced under a waiter.
    with _registry_lock:
        entry = _environments.setdefault(vm_id, _Entry())
        entry.users += 1
    try:
        with entry.lock:
            yield
    finally:
        with _registry_lock:
            entry.users -= 1
            if not entry.users:
                del _environments[vm_id]


def serialize_environment(function):
    @wraps(function)
    def guarded(self, vm_id, *args, **kwargs):
        with environment_lock(vm_id):
            return function(self, vm_id, *args, **kwargs)

    return guarded


def serialize_creation(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        # Include rollback and publication in the capacity check's critical section.
        with _allocations:
            return function(*args, **kwargs)

    return guarded
