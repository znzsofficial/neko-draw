"""Process-local bounded image work; cancellation never frees a live worker slot."""
import asyncio
from contextlib import asynccontextmanager
import time
from weakref import WeakKeyDictionary

MAX_ADMITTED = 8
MAX_GENERATING = 2
MAX_PROMPT_CHARS = 4000
_workers = WeakKeyDictionary()


@asynccontextmanager
async def deadline_slot(slot, deadline):
    """Deadline only the wait, never cancel a worker inside the acquired slot."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('admission expired')
    async with asyncio.timeout(remaining):
        await slot.acquire()
    try:
        yield
    finally:
        slot.release()


async def thread_work(func, *args, **kwargs):
    """Keep the caller's locks/budgets until its real thread completes.

    No lock is acquired here: the worker must not wait on an asyncio lock held
    by its caller. Repeated cancellation remains cancellation, not worker error.
    """
    worker = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
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
            worker.exception()
        raise


async def image_work(func, *args):
    loop = asyncio.get_running_loop()
    slots = _workers.setdefault(loop, asyncio.Semaphore(2))
    async with slots:
        return await thread_work(func, *args)
