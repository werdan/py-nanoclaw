from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nanoclaw.loop import run_worker_loop
from nanoclaw.models import Inbound
from nanoclaw.session import load_session_id, save_session_id


def test_load_session_missing(tmp_path: Path) -> None:
    assert load_session_id(tmp_path / "nope.txt") is None


def test_load_save_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "session.txt"
    assert load_session_id(path) is None
    save_session_id(path, "sdk-session-abc")
    assert load_session_id(path) == "sdk-session-abc"
    assert path.read_text(encoding="utf-8").strip() == "sdk-session-abc"


@pytest.mark.asyncio
async def test_worker_batches_and_task_done() -> None:
    q: asyncio.Queue[Inbound] = asyncio.Queue()
    seen: list[list[Inbound]] = []

    async def dispatch(batch: list[Inbound]) -> None:
        seen.append(list(batch))

    async def producer() -> None:
        await q.put(Inbound("a"))
        await q.put(Inbound("b"))
        await asyncio.sleep(0.05)

    stop = asyncio.Event()
    worker = asyncio.create_task(
        run_worker_loop(q, dispatch, wait_timeout_s=0.05, stop=stop)
    )
    await producer()
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(worker, timeout=2.0)

    assert len(seen) >= 1
    assert {m.content for m in seen[0]} == {"a", "b"}
    assert q.empty()


@pytest.mark.asyncio
async def test_failed_dispatch_drops_batch() -> None:
    q: asyncio.Queue[Inbound] = asyncio.Queue()
    await q.put(Inbound("x"))

    def dispatch(_: list[Inbound]) -> None:
        raise RuntimeError("down")

    stop = asyncio.Event()

    async def killer() -> None:
        await asyncio.sleep(0.15)
        stop.set()

    await asyncio.gather(
        run_worker_loop(q, dispatch, wait_timeout_s=0.05, stop=stop),
        killer(),
    )

    assert q.empty()


@pytest.mark.asyncio
async def test_queue_join_completes_after_successful_dispatch() -> None:
    """Producer join() must unblock: one task_done per get() after dispatch returns."""
    q: asyncio.Queue[Inbound] = asyncio.Queue()
    await q.put(Inbound("a"))

    async def dispatch(_: list[Inbound]) -> None:
        pass

    stop = asyncio.Event()
    worker = asyncio.create_task(
        run_worker_loop(q, dispatch, wait_timeout_s=0.05, stop=stop)
    )
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(worker, timeout=2.0)

    await asyncio.wait_for(q.join(), timeout=1.0)


@pytest.mark.asyncio
async def test_queue_join_completes_after_failed_dispatch() -> None:
    """Same pairing when dispatch drops the batch — omitting task_done on error would hang join()."""
    q: asyncio.Queue[Inbound] = asyncio.Queue()
    await q.put(Inbound("a"))
    await q.put(Inbound("b"))

    def dispatch(_: list[Inbound]) -> None:
        raise RuntimeError("fail")

    stop = asyncio.Event()
    worker = asyncio.create_task(
        run_worker_loop(q, dispatch, wait_timeout_s=0.05, stop=stop)
    )
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(worker, timeout=2.0)

    await asyncio.wait_for(q.join(), timeout=1.0)
