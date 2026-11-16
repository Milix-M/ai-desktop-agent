"""Run blocking display calls without blocking the server event loop."""

import asyncio
import threading
from collections.abc import Callable
from functools import partial


async def run_display_call[T](fn: Callable[..., T], *args, **kwargs) -> T:
    # Drain an in-flight operation before allowing another task to own this display.
    # Cancelling a to_thread await alone does not stop the underlying thread.
    worker = asyncio.create_task(asyncio.to_thread(partial(fn, *args, **kwargs)))
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


_VM_OPERATIONS = threading.RLock()


async def run_vm_call[T](fn: Callable[..., T], *args, **kwargs) -> T:
    # Port allocation and pool metadata are shared. Keep Docker operations serialized.
    def operation():
        with _VM_OPERATIONS:
            return fn(*args, **kwargs)

    return await run_display_call(operation)
