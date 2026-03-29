from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from nanoclaw.models import Inbound

logger = logging.getLogger(__name__)


async def run_worker_loop(
    queue: asyncio.Queue[Inbound],
    dispatch: Callable[[list[Inbound]], Awaitable[None]],
    *,
    wait_timeout_s: float = 0.5,
    stop: asyncio.Event | None = None,
) -> None:
    """
    Wait for inbound items on ``queue``, drain any additional items already queued into one
    batch, then call ``dispatch``.

    ``dispatch`` is async. If it raises, the batch is logged and dropped (no retry).

    **Queue contract:** each :meth:`~asyncio.Queue.get` / :meth:`~asyncio.Queue.get_nowait` must be
    followed by exactly one :meth:`~asyncio.Queue.task_done` so :meth:`~asyncio.Queue.join` can
    complete. That pairing happens in ``finally`` (do not re-``put`` failed items: that would
    desynchronize ``put``/``task_done`` counts).

    Pass ``stop`` and ``stop.set()`` from elsewhere to exit (checked between waits).
    """
    while stop is None or not stop.is_set():
        try:
            first = await asyncio.wait_for(queue.get(), timeout=wait_timeout_s)
        except TimeoutError:
            continue

        batch: list[Inbound] = [first]
        while True:
            try:
                batch.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        try:
            await dispatch(batch)
        except Exception:
            logger.exception(
                "dispatch failed; dropping batch of %d inbound item(s)", len(batch)
            )
        finally:
            for _ in batch:
                queue.task_done()
