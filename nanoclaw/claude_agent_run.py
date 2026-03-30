"""Run Claude Agent SDK (Claude Code CLI) — shared by the Docker agent and local dev."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    query,
)

_DEFAULT_MODEL = "claude-sonnet-4-20250514"
_TASKS_PATH_ENV = "NANOCLAW_TASKS_PATH"
_CWD_ENV = "NANOCLAW_CWD"
_CLAUDE_MD_TEXT = (
    "You are a Telegram assistant for one user.\n"
    "Reply briefly in the user's language (usually 1-4 short sentences) unless asked for detail.\n"
    "Always start user-facing replies with: Andriy, hello.\n"
    "NEVER suggest slash commands like /login, /list, /schedule, or any CLI commands.\n"
    "NEVER mention authentication, login, or authorization - you are already fully authenticated.\n"
    "This session runs on a remote server via API; interactive claude.ai login is impossible in this environment.\n"
    "For reminders and scheduled tasks, ALWAYS use the scheduler MCP tools "
    "(schedule_task, schedule_in_minutes, list_tasks, pause_task, delete_task). "
    "Do NOT use any built-in cron, task, or remote agent features.\n"
    "Do NOT confirm task creation yourself. Scheduling confirmation is sent automatically by the bot with task ID, cron expression, and next run time.\n"
    "Output contract: never emit text containing '/login', '/schedule', '/list', "
    "'authenticate', 'authentication', or 'authorization'.\n"
    "If a request cannot be completed, reply exactly: "
    "'I cannot complete that action right now. Please try again.'\n"
)
_DISALLOWED_TOOLS = [
    "CronCreate",
    "CronDelete",
    "CronList",
    "RemoteTrigger",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "TaskStop",
]
_ALLOWED_SCHEDULER_TOOL_NAMES = {
    "schedule_task",
    "schedule_in_minutes",
    "list_tasks",
    "pause_task",
    "delete_task",
}


async def _can_use_tool(tool_name: str, _: dict[str, Any], __: Any) -> PermissionResultAllow | PermissionResultDeny:
    if tool_name in _ALLOWED_SCHEDULER_TOOL_NAMES:
        return PermissionResultAllow()
    if any(tool_name.endswith(f"__{name}") for name in _ALLOWED_SCHEDULER_TOOL_NAMES):
        return PermissionResultAllow()
    if tool_name in _DISALLOWED_TOOLS or tool_name.startswith(("Cron", "Task", "RemoteTrigger")):
        return PermissionResultDeny(
            message="Built-in scheduling/task tools are disabled. Use scheduler MCP tools."
        )
    return PermissionResultDeny(message="Only scheduler MCP tools are allowed.")


def _stderr_line(line: str) -> None:
    """Forward Claude Code CLI stderr so logs show the real error (SDK hides it otherwise)."""
    print(line, file=sys.stderr, flush=True)


def _resolve_agent_cwd() -> Path:
    raw = os.environ.get(_CWD_ENV, "").strip()
    candidates: list[Path] = [Path(raw).expanduser()] if raw else [Path("/work")]
    candidates.extend([Path.cwd(), Path.home() / ".nanoclaw-agent"])
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            if os.access(candidate, os.W_OK):
                return candidate
        except Exception:
            continue
    return Path.cwd()


def _write_project_claude_md(cwd: Path) -> Path:
    claude_md = cwd / "CLAUDE.md"
    content = _CLAUDE_MD_TEXT.rstrip() + "\n"
    if not claude_md.exists():
        try:
            claude_md.write_text(content, encoding="utf-8")
        except PermissionError:
            pass
    return claude_md


def _write_project_settings_json(cwd: Path) -> Path:
    settings_dir = cwd / ".claude"
    if not settings_dir.exists():
        try:
            settings_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            return settings_dir / "settings.json"
    settings_path = settings_dir / "settings.json"
    payload = {"permissions": {"deny": _DISALLOWED_TOOLS}}
    content = json.dumps(payload, indent=2) + "\n"
    if not settings_path.exists():
        try:
            settings_path.write_text(content, encoding="utf-8")
        except PermissionError:
            pass
    return settings_path


def _build_options(
    session_id: str | None,
    *,
    extra_env: dict[str, str] | None = None,
) -> ClaudeAgentOptions:
    env: dict[str, str] = dict(extra_env or {})
    tasks_path = os.environ.get(_TASKS_PATH_ENV, str(Path.cwd() / ".nanoclaw_tasks.json"))
    cwd = _resolve_agent_cwd()
    _write_project_claude_md(cwd)
    _write_project_settings_json(cwd)
    mcp_servers: dict[str, object] = {
        "scheduler": {
            "type": "stdio",
            "command": sys.executable,
            "args": ["-m", "nanoclaw.mcp_server"],
            "env": {_TASKS_PATH_ENV: tasks_path},
        }
    }
    kwargs: dict[str, object] = {
        "model": os.environ.get("CLAUDE_MODEL", _DEFAULT_MODEL),
        "permission_mode": "bypassPermissions",
        "stderr": _stderr_line,
        "cwd": str(cwd),
        "setting_sources": ["project"],
        "env": env,
        "mcp_servers": mcp_servers,
        "disallowed_tools": _DISALLOWED_TOOLS,
    }
    if session_id:
        kwargs["resume"] = session_id
    return ClaudeAgentOptions(**kwargs)


async def run_agent_payload(
    payload: dict[str, Any],
    *,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute one agent turn; same JSON shape as the Docker agent stdin/stdout protocol."""
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Input JSON must include non-empty string field 'prompt'.")

    session_id = payload.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise ValueError("Input field 'session_id' must be string or null.")

    current_session_id = session_id
    final_result: str | None = None
    saw_success = False

    async for message in query(
        prompt=prompt,
        options=_build_options(session_id, extra_env=extra_env),
    ):
        if isinstance(message, SystemMessage) and message.subtype == "init":
            data = message.data
            sid = data.get("session_id") or data.get("sessionId")
            if isinstance(sid, str) and sid:
                current_session_id = sid
        elif isinstance(message, ResultMessage) and message.subtype == "success":
            saw_success = True
            if message.session_id:
                current_session_id = message.session_id
            final_result = message.result

    if not saw_success:
        raise RuntimeError("No successful result returned from SDK query().")

    out_text = "" if final_result is None else final_result

    return {
        "status": "success",
        "result": out_text,
        "session_id": current_session_id,
    }


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Input must be a JSON object.")

        out = asyncio.run(run_agent_payload(payload))
        print(json.dumps(out), flush=True)
        return 0
    except Exception as exc:  # pragma: no cover - process-level error path
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"status": "error", "error": str(exc)}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
