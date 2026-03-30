"""Integration tests for dispatch wiring and worker loop behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import nanoclaw.dispatch as dispatch_mod
from nanoclaw.dispatch import dispatch as agent_dispatch
from nanoclaw.loop import run_worker_loop
from nanoclaw.models import Inbound
from nanoclaw.telegram_app import cleanup_inbound_temp_files


@pytest.fixture(autouse=True)
def _clear_local_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests in this module assume HTTP-path dispatch unless explicitly enabled."""
    monkeypatch.delenv("NANOCLAW_AGENT_LOCAL", raising=False)


@pytest.mark.asyncio
async def test_dispatch_persists_session_and_enqueues_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``dispatch()`` persists session + pushes result from HTTP agent output."""
    session_path = tmp_path / "session.txt"
    out_queue: asyncio.Queue[str] = asyncio.Queue()
    session_ref: list[str | None] = [None]
    seen_payload: dict[str, object] = {}

    async def fake_run_agent_http(payload: dict[str, object]):
        seen_payload.update(payload)
        return {"status": "success", "session_id": "from-result", "result": "assistant text"}

    monkeypatch.setattr("nanoclaw.dispatch._run_agent_http", fake_run_agent_http)

    await agent_dispatch([Inbound("hello")], out_queue, session_ref, session_path)

    assert seen_payload == {"prompt": "hello", "session_id": None}
    assert session_path.read_text(encoding="utf-8").strip() == "from-result"
    assert session_ref[0] == "from-result"
    assert await out_queue.get() == "assistant text"


@pytest.mark.asyncio
async def test_run_worker_loop_cli_style_handle_batch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Same wiring as ``cli._run``: ``handle_batch`` closes over ``out_queue``, ``session_ref``, ``session_path``."""
    session_path = tmp_path / ".nanoclaw_session"
    out_queue: asyncio.Queue[str] = asyncio.Queue()
    session_ref: list[str | None] = [None]

    async def fake_run_agent_http(payload: dict[str, object]):
        prompt = payload["prompt"]
        return {"status": "success", "session_id": "loop-test", "result": f"echo:{prompt}"}

    monkeypatch.setattr("nanoclaw.dispatch._run_agent_http", fake_run_agent_http)

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


def test_cleanup_inbound_temp_files(tmp_path: Path) -> None:
    temp_a = tmp_path / "a.jpg"
    temp_b = tmp_path / "b.jpg"
    temp_a.write_text("a", encoding="utf-8")
    temp_b.write_text("b", encoding="utf-8")
    batch = [
        Inbound("with file a", temp_paths=(temp_a,)),
        Inbound("with file b", temp_paths=(temp_b,)),
        Inbound("no files"),
    ]

    cleanup_inbound_temp_files(batch)

    assert not temp_a.exists()
    assert not temp_b.exists()


@pytest.mark.asyncio
async def test_dispatch_fails_fast_on_error_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Dispatch is fail-fast: non-success status raises immediately."""
    out_queue: asyncio.Queue[str] = asyncio.Queue()
    session_ref: list[str | None] = [None]
    session_path = tmp_path / "session.txt"

    async def fake_run_agent_http(payload: dict[str, object]):
        return {"status": "error", "error": "transient"}

    monkeypatch.setattr("nanoclaw.dispatch._run_agent_http", fake_run_agent_http)

    with pytest.raises(RuntimeError, match="status is not success"):
        await agent_dispatch([Inbound("hello")], out_queue, session_ref, session_path)


@pytest.mark.asyncio
async def test_dispatch_clears_stale_session_and_retries_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Dispatch clears persisted stale session and retries once without resume."""
    out_queue: asyncio.Queue[str] = asyncio.Queue()
    session_ref: list[str | None] = ["stale-session"]
    session_path = tmp_path / "session.txt"
    session_path.write_text("stale-session", encoding="utf-8")
    calls: list[dict[str, object]] = []

    async def fake_run_agent_http(payload: dict[str, object]):
        calls.append(payload)
        if payload.get("session_id") == "stale-session":
            raise RuntimeError("resume initialize failed")
        return {"status": "success", "session_id": "fresh-session", "result": "recovered"}

    monkeypatch.setattr("nanoclaw.dispatch._run_agent_http", fake_run_agent_http)
    await agent_dispatch([Inbound("hello")], out_queue, session_ref, session_path)
    assert calls == [
        {"prompt": "hello", "session_id": "stale-session"},
        {"prompt": "hello", "session_id": None},
    ]
    assert session_ref[0] == "fresh-session"
    assert session_path.read_text(encoding="utf-8").strip() == "fresh-session"
    assert await out_queue.get() == "recovered"


@pytest.mark.asyncio
async def test_dispatch_skips_queue_when_result_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Empty or whitespace-only agent result does not enqueue a reply."""
    out_queue: asyncio.Queue[str] = asyncio.Queue()
    session_ref: list[str | None] = [None]
    session_path = tmp_path / "session.txt"

    async def fake_run_agent_http(payload: dict[str, object]):
        return {"status": "success", "session_id": "s1", "result": ""}

    monkeypatch.setattr("nanoclaw.dispatch._run_agent_http", fake_run_agent_http)

    await agent_dispatch([Inbound("hello")], out_queue, session_ref, session_path)

    assert out_queue.empty()


@pytest.mark.asyncio
async def test_dispatch_local_agent_skips_docker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With NANOCLAW_AGENT_LOCAL, dispatch calls _run_agent_local instead of HTTP."""
    session_path = tmp_path / "session.txt"
    out_queue: asyncio.Queue[str] = asyncio.Queue()
    session_ref: list[str | None] = [None]
    seen: dict[str, object] = {}

    async def fake_run_agent_local(payload: dict[str, object]) -> dict[str, object]:
        seen.update(payload)
        return {"status": "success", "session_id": "local-sid", "result": "local ok"}

    monkeypatch.setattr("nanoclaw.dispatch._run_agent_local", fake_run_agent_local)
    monkeypatch.setenv("NANOCLAW_AGENT_LOCAL", "1")

    await agent_dispatch([Inbound("hello")], out_queue, session_ref, session_path)

    assert seen == {"prompt": "hello", "session_id": None}
    assert await out_queue.get() == "local ok"
    assert session_path.read_text(encoding="utf-8").strip() == "local-sid"


@pytest.mark.asyncio
async def test_dispatch_raises_on_container_error_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out_queue: asyncio.Queue[str] = asyncio.Queue()
    session_ref: list[str | None] = [None]
    session_path = tmp_path / "session.txt"

    async def fake_run_agent_http(payload: dict[str, object]):
        return {"status": "error", "error": "boom"}

    monkeypatch.setattr("nanoclaw.dispatch._run_agent_http", fake_run_agent_http)

    with pytest.raises(RuntimeError, match="status is not success"):
        await agent_dispatch([Inbound("hello")], out_queue, session_ref, session_path)


@pytest.mark.asyncio
async def test_run_agent_http_timeout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOCLAW_AGENT_URL", "http://agent:8000/message")
    monkeypatch.setenv("NANOCLAW_AGENT_TIMEOUT_S", "0.01")

    def fake_urlopen(*args: object, **kwargs: object) -> object:
        raise TimeoutError("timeout")

    monkeypatch.setattr(dispatch_mod, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="agent HTTP request failed"):
        await dispatch_mod._run_agent_http({"prompt": "x", "session_id": None})
