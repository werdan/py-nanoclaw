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
    """Tests in this module assume docker-path dispatch unless explicitly enabled."""
    monkeypatch.delenv("NANOCLAW_AGENT_LOCAL", raising=False)


@pytest.mark.asyncio
async def test_dispatch_persists_session_and_enqueues_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``dispatch()`` persists session + pushes result from container output."""
    session_path = tmp_path / "session.txt"
    out_queue: asyncio.Queue[str] = asyncio.Queue()
    session_ref: list[str | None] = [None]
    seen_payload: dict[str, object] = {}

    async def fake_run_agent_container(*, payload: dict[str, object], session_path: Path, temp_paths: tuple[Path, ...]):
        assert session_path.name == "session.txt"
        assert temp_paths == ()
        seen_payload.update(payload)
        return {"status": "success", "session_id": "from-result", "result": "assistant text"}

    monkeypatch.setattr("nanoclaw.dispatch._run_agent_container", fake_run_agent_container)

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

    async def fake_run_agent_container(*, payload: dict[str, object], session_path: Path, temp_paths: tuple[Path, ...]):
        prompt = payload["prompt"]
        return {"status": "success", "session_id": "loop-test", "result": f"echo:{prompt}"}

    monkeypatch.setattr("nanoclaw.dispatch._run_agent_container", fake_run_agent_container)

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

    async def fake_run_agent_container(*, payload: dict[str, object], session_path: Path, temp_paths: tuple[Path, ...]):
        return {"status": "error", "error": "transient"}

    monkeypatch.setattr("nanoclaw.dispatch._run_agent_container", fake_run_agent_container)

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

    async def fake_run_agent_container(*, payload: dict[str, object], session_path: Path, temp_paths: tuple[Path, ...]):
        calls.append(payload)
        if payload.get("session_id") == "stale-session":
            raise RuntimeError("resume initialize failed")
        return {"status": "success", "session_id": "fresh-session", "result": "recovered"}

    monkeypatch.setattr("nanoclaw.dispatch._run_agent_container", fake_run_agent_container)
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

    async def fake_run_agent_container(*, payload: dict[str, object], session_path: Path, temp_paths: tuple[Path, ...]):
        return {"status": "success", "session_id": "s1", "result": ""}

    monkeypatch.setattr("nanoclaw.dispatch._run_agent_container", fake_run_agent_container)

    await agent_dispatch([Inbound("hello")], out_queue, session_ref, session_path)

    assert out_queue.empty()


@pytest.mark.asyncio
async def test_dispatch_local_agent_skips_docker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With NANOCLAW_AGENT_LOCAL, dispatch calls _run_agent_local instead of docker."""
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

    async def fake_run_agent_container(*, payload: dict[str, object], session_path: Path, temp_paths: tuple[Path, ...]):
        return {"status": "error", "error": "boom"}

    monkeypatch.setattr("nanoclaw.dispatch._run_agent_container", fake_run_agent_container)

    with pytest.raises(RuntimeError, match="status is not success"):
        await agent_dispatch([Inbound("hello")], out_queue, session_ref, session_path)


def test_docker_env_args_passes_onecli_and_claude_model_not_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent env wiring uses OneCLI-provided env and never forwards host Anthropic key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    monkeypatch.setenv("CLAUDE_MODEL", "model-x")
    monkeypatch.setenv("ONECLI_URL", "http://onecli:10255")
    monkeypatch.setenv("ONECLI_API_KEY", "oc_onecli-key")
    monkeypatch.delenv("NANOCLAW_ONECLI_PROXY_DISABLE", raising=False)

    args = dispatch_mod._docker_env_args(
        onecli_env={
            "HTTPS_PROXY": "http://x:aoc_real-agent@host.docker.internal:10255",
            "ANTHROPIC_API_KEY": "placeholder",
        }
    )
    joined = " ".join(args)

    assert "-e" in args
    assert "CLAUDE_MODEL" in args
    assert "HTTPS_PROXY=http://x:aoc_real-agent@host.docker.internal:10255" in joined
    assert "ANTHROPIC_API_KEY=placeholder" in joined
    assert "ONECLI_API_KEY=" not in joined
    assert "ant-key" not in joined


def test_docker_env_args_legacy_onecli_passthrough_when_proxy_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NANOCLAW_ONECLI_PROXY_DISABLE", "1")
    monkeypatch.setenv("ONECLI_URL", "http://onecli:10255")
    monkeypatch.setenv("ONECLI_API_KEY", "oc_legacy")
    args = dispatch_mod._docker_env_args()
    joined = " ".join(args)
    assert "HTTPS_PROXY" not in joined
    assert "ONECLI_API_KEY" in joined


def test_project_instruction_volume_args_mounts_claude_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    (project / "runtime" / "sessions").mkdir(parents=True)
    (project / "CLAUDE.md").write_text("rules", encoding="utf-8")
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("NANOCLAW_DOCKER_HOST_SESSION_DIR", str(project / "runtime" / "sessions"))
    monkeypatch.delenv("NANOCLAW_DOCKER_HOST_PROJECT_DIR", raising=False)

    mounts = dispatch_mod._project_instruction_volume_args(
        session_path=Path("/runtime/sessions/.nanoclaw_session")
    )
    joined = " ".join(mounts)

    assert f"{project / 'CLAUDE.md'}:/work/CLAUDE.md:ro" in joined
    assert f"{project / '.claude'}:/work/.claude:ro" in joined


def test_onecli_api_base_url_swaps_gateway_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NANOCLAW_ONECLI_API_URL", raising=False)
    monkeypatch.setenv("ONECLI_URL", "http://onecli:10255")
    assert dispatch_mod._onecli_api_base_url() == "http://onecli:10254"


def test_onecli_api_base_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOCLAW_ONECLI_API_URL", "http://onecli:18080")
    monkeypatch.setenv("ONECLI_URL", "http://onecli:10255")
    assert dispatch_mod._onecli_api_base_url() == "http://onecli:18080"


def test_fetch_container_config_uses_agent_query(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResp:
        def __enter__(self) -> "FakeResp":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        @property
        def status(self) -> int:
            return 200

        def read(self) -> bytes:
            return b'{"env":{"HTTPS_PROXY":"http://x:aoc@host:10255"}}'

    seen: dict[str, str] = {}

    def fake_urlopen(req: object, timeout: int = 0) -> FakeResp:
        seen["url"] = req.full_url  # type: ignore[attr-defined]
        return FakeResp()

    monkeypatch.setenv("ONECLI_API_KEY", "oc_key")
    monkeypatch.setenv("NANOCLAW_ONECLI_API_URL", "http://onecli:10254")
    monkeypatch.setenv("NANOCLAW_ONECLI_AGENT", "claude-main-agent")
    monkeypatch.setattr(dispatch_mod, "urlopen", fake_urlopen)

    cfg = dispatch_mod._fetch_onecli_container_config()
    assert "agent=claude-main-agent" in seen["url"]
    assert cfg["env"]["HTTPS_PROXY"] == "http://x:aoc@host:10255"


def test_onecli_runtime_for_docker_uses_api_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ONECLI_URL", "http://onecli:10255")
    monkeypatch.setenv("ONECLI_API_KEY", "oc_key")
    monkeypatch.delenv("NANOCLAW_ONECLI_PROXY_DISABLE", raising=False)
    monkeypatch.setenv("NANOCLAW_ANTHROPIC_PLACEHOLDER_KEY", "placeholder-x")

    monkeypatch.setattr(
        dispatch_mod,
        "_fetch_onecli_container_config",
        lambda: {
            "env": {"HTTPS_PROXY": "http://x:aoc_agent@host.docker.internal:10255"},
            "ca_pem": "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n",
            "ca_path": "/tmp/onecli-gateway-ca.pem",
        },
    )

    env, mounts, cleanup = dispatch_mod._onecli_runtime_for_docker(
        session_path=tmp_path / "session.txt"
    )

    assert env["HTTPS_PROXY"] == "http://x:aoc_agent@host.docker.internal:10255"
    assert mounts[0] == "-v"
    assert mounts[1].endswith(":/tmp/onecli-gateway-ca.pem:ro")
    assert len(cleanup) == 1
    # cleanup temp file created by runtime helper
    cleanup[0].unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_run_agent_container_kills_process_on_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeProc:
        def __init__(self) -> None:
            self.returncode = 0
            self.killed = False

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            if self.killed:
                return b"", b"forced stop"
            await asyncio.sleep(0.05)
            return b'{"status":"success"}', b""

        def kill(self) -> None:
            self.killed = True

    fake_proc = FakeProc()

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProc:
        return fake_proc

    monkeypatch.setattr(dispatch_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setenv("NANOCLAW_AGENT_TIMEOUT_S", "0.001")

    with pytest.raises(RuntimeError, match="timed out"):
        await dispatch_mod._run_agent_container(
            payload={"prompt": "hello", "session_id": None},
            session_path=tmp_path / "session.txt",
            temp_paths=(),
        )

    assert fake_proc.killed is True
