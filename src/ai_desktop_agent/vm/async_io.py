"""Run blocking display calls without blocking the server event loop."""

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial


async def run_display_call[T](fn: Callable[..., T], *args, **kwargs) -> T:
    # Drain an in-flight operation before allowing another task to own this display.
    # Cancelling a to_thread await alone does not stop the underlying thread.
    worker = asyncio.create_task(asyncio.to_thread(partial(fn, *args, **kwargs)))
    return await _wait_for_worker(worker)


async def _wait_for_worker[T](worker: asyncio.Future[T]) -> T:
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()  # retrieve failures while preserving cancellation
        raise


def vm_call[T](fn: Callable[..., T], *args, **kwargs) -> T:
    # Mutating VM services own their environment/allocation locks; reads may run concurrently.
    return fn(*args, **kwargs)


async def run_vm_call[T](fn: Callable[..., T], *args, **kwargs) -> T:
    return await run_display_call(vm_call, fn, *args, **kwargs)


class VmCreationBusyError(RuntimeError):
    """The process already accepted its maximum creation backlog."""


_VM_CREATIONS = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vm-creation")
MAX_PENDING_VM_CREATIONS = 8
_VM_CREATION_SLOTS = threading.BoundedSemaphore(MAX_PENDING_VM_CREATIONS)


class VmCreationSlot:
    def __init__(self, semaphore: threading.BoundedSemaphore):
        self._semaphore = semaphore
        self._guard = threading.Lock()
        self.released = False

    def release(self) -> None:
        with self._guard:
            if not self.released:
                self.released = True
                self._semaphore.release()


def reserve_vm_creation_slot() -> VmCreationSlot:
    if not _VM_CREATION_SLOTS.acquire(blocking=False):
        raise VmCreationBusyError(
            "環境の作成要求が混み合っています。しばらく待って再操作してください"
        )
    return VmCreationSlot(_VM_CREATION_SLOTS)


async def run_reserved_vm_creation[T](
    slot: VmCreationSlot, fn: Callable[..., T], *args, **kwargs
) -> T:
    if slot.released:
        raise RuntimeError("creation slot was already released")
    worker = asyncio.get_running_loop().run_in_executor(_VM_CREATIONS, partial(fn, *args, **kwargs))
    return await _wait_for_worker(worker)


async def run_vm_creation[T](fn: Callable[..., T], *args, **kwargs) -> T:
    # Waiting for serialized allocation must not occupy the executor used by reads and saves.
    slot = reserve_vm_creation_slot()
    try:
        return await run_reserved_vm_creation(slot, fn, *args, **kwargs)
    finally:
        slot.release()
