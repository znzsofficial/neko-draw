"""Process-local bounded image work; cancellation never frees a live worker slot."""
import asyncio
from weakref import WeakKeyDictionary

MAX_ADMITTED = 8
MAX_GENERATING = 2
MAX_PROMPT_CHARS = 4000
_workers = WeakKeyDictionary()


async def image_work(func, *args):
    loop = asyncio.get_running_loop()
    slots = _workers.setdefault(loop, asyncio.Semaphore(2))
    async with slots:
        worker = asyncio.create_task(asyncio.to_thread(func, *args))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Threads cannot be cancelled: retain the budget until they stop.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            # Observe thread errors without replacing the caller cancellation.
            if not worker.cancelled():
                worker.exception()
            raise
