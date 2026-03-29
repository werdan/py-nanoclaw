"""Integration tests for ``nanoclaw.dispatch`` and the cli-style ``handle_batch`` closure."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage, SystemMessage

from nanoclaw.dispatch import dispatch as agent_dispatch
from nanoclaw.loop import run_worker_loop
from nanoclaw.models import Inbound


def _success_result(*, session_id: str, result: str) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=0,
        duration_api_ms=0,
        is_error=False,
        num_turns=1,
        session_id=session_id,
        result=result,
    )


@pytest.mark.asyncio
async def test_dispatch_persists_session_and_enqueues_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Real ``dispatch()`` with ``query`` patched: session file + ``session_ref`` + ``out_queue``."""
    session_path = tmp_path / "session.txt"
    out_queue: asyncio.Queue[str] = asyncio.Queue()
    session_ref: list[str | None] = [None]

    async def fake_query(*, prompt: str, options: object):
        assert "hello" in prompt
        yield SystemMessage(subtype="init", data={"session_id": "from-init"})
        yield _success_result(session_id="from-result", result="assistant text")

    monkeypatch.setattr("nanoclaw.dispatch.query", fake_query)

    await agent_dispatch([Inbound("hello")], out_queue, session_ref, session_path)

    assert session_path.read_text(encoding="utf-8").strip() == "from-result"
    assert session_ref[0] == "from-result"
    assert await out_queue.get() == "assistant text"


@pytest.mark.asyncio
async def test_run_worker_loop_cli_style_handle_batch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Same wiring as ``cli._run``: ``handle_batch`` closes over ``out_queue``, ``session_ref``, ``session_path``."""
    session_path = tmp_path / ".nanoclaw_session"
    out_queue: asyncio.Queue[str] = asyncio.Queue()
    session_ref: list[str | None] = [None]

    async def fake_query(*, prompt: str, options: object):
        yield _success_result(session_id="loop-test", result=f"echo:{prompt}")

    monkeypatch.setattr("nanoclaw.dispatch.query", fake_query)

    async def handle_batch(batch: list[Inbound]) -> None:
        await agent_dispatch(batch, out_queue, session_ref, session_path)

    inbound: asyncio.Queue[Inbound] = asyncio.Queue()
    stop = asyncio.Event()
    await inbound.put(Inbound("ping"))

    worker = asyncio.create_task(
        run_worker_loop(inbound, handle_batch, wait_timeout_s=0.05, stop=stop)
    )
    await asyncio.sleep(0.25)
    stop.set()
    await asyncio.wait_for(worker, timeout=2.0)

    assert await out_queue.get() == "echo:ping"
    assert session_ref[0] == "loop-test"
    assert session_path.read_text(encoding="utf-8").strip() == "loop-test"


def test_telegram_app_module_imports() -> None:
    """Smoke-import so wiring stays valid (requires python-telegram-bot)."""
    import nanoclaw.telegram_app  # noqa: F401
