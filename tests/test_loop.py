from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from nanoclaw.dispatch import load_session_id, save_session_id
from nanoclaw.loop import run_worker_loop
from nanoclaw.models import Inbound


def test_load_session_missing(tmp_path: Path) -> None:
    assert load_session_id(tmp_path / "nope.txt") is None


def test_load_save_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "session.txt"
    assert load_session_id(path) is None
    save_session_id(path, "sdk-session-abc")
    assert load_session_id(path) == "sdk-session-abc"
    assert path.read_text(encoding="utf-8").strip() == "sdk-session-abc"


@pytest.mark.asyncio
async def test_worker_e2e_batching_and_dispatch(capsys: pytest.CaptureFixture[str]) -> None:
    """
    End-to-end: two inbound messages, dispatch logs each batch then each line (like a real agent).

    Asserts (1) every message appears in output and (2) batch sizes sum to 2 — whether the worker
    drains one batch of two or two batches of one depends on scheduling.
    """
    queue: asyncio.Queue[Inbound] = asyncio.Queue()
    stop = asyncio.Event()

    async def dispatch(batch: list[Inbound]) -> None:
        print(f"dispatch: {len(batch)} message(s)")
        for msg in batch:
            print(f"  Agent got: {msg.content}")

    async def producer() -> None:
        await queue.put(Inbound("a"))
        await queue.put(Inbound("b"))

    worker = asyncio.create_task(
        run_worker_loop(queue, dispatch, wait_timeout_s=0.05, stop=stop)
    )
    await producer()
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(worker, timeout=2.0)

    out = capsys.readouterr().out
    assert queue.empty()

    contents = re.findall(r"Agent got: (.+)", out)
    assert sorted(contents) == ["a", "b"]

    batch_sizes = [int(m.group(1)) for m in re.finditer(r"dispatch: (\d+) message", out)]
    assert sum(batch_sizes) == 2


@pytest.mark.asyncio
async def test_failed_dispatch_drops_batch() -> None:
    q: asyncio.Queue[Inbound] = asyncio.Queue()
    await q.put(Inbound("x"))

    async def dispatch(_: list[Inbound]) -> None:
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

    async def dispatch(_: list[Inbound]) -> None:
        raise RuntimeError("fail")

    stop = asyncio.Event()
    worker = asyncio.create_task(
        run_worker_loop(q, dispatch, wait_timeout_s=0.05, stop=stop)
    )
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(worker, timeout=2.0)

    await asyncio.wait_for(q.join(), timeout=1.0)
