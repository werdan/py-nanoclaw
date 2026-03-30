from __future__ import annotations

import json
from pathlib import Path

from nanoclaw.claude_agent_run import _build_options


def test_build_options_includes_scheduler_mcp(monkeypatch) -> None:
    monkeypatch.setenv("NANOCLAW_TASKS_PATH", "/tmp/custom-tasks.json")
    opts = _build_options(None, extra_env={"X": "1"})

    mcp_servers = opts.mcp_servers
    assert isinstance(mcp_servers, dict)
    assert "scheduler" in mcp_servers
    cfg = mcp_servers["scheduler"]
    assert cfg["command"]
    assert cfg["args"] == ["-m", "nanoclaw.mcp_server"]
    assert cfg["env"]["NANOCLAW_TASKS_PATH"] == "/tmp/custom-tasks.json"


def test_build_options_sets_default_tasks_path(monkeypatch) -> None:
    monkeypatch.delenv("NANOCLAW_TASKS_PATH", raising=False)
    opts = _build_options(None, extra_env={})
    cfg = opts.mcp_servers["scheduler"]
    assert cfg["env"]["NANOCLAW_TASKS_PATH"] == str(Path.cwd() / ".nanoclaw_tasks.json")


def test_build_options_sets_brief_behavior_system_prompt() -> None:
    opts = _build_options(None, extra_env={})
    assert opts.setting_sources == ["project"]
    assert opts.cwd is not None
    claude_md = Path(str(opts.cwd)) / "CLAUDE.md"
    assert claude_md.exists()
    text = claude_md.read_text(encoding="utf-8")
    assert "Reply briefly" in text


def test_build_options_does_not_write_claude_md_to_configured_cwd(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NANOCLAW_CWD", str(tmp_path))
    opts = _build_options(None, extra_env={})
    assert Path(str(opts.cwd)) == tmp_path
    assert not (tmp_path / "CLAUDE.md").exists()
    settings_path = tmp_path / ".claude" / "settings.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "CronCreate" in settings["permissions"]["deny"]
    assert "TaskStop" in settings["permissions"]["deny"]


