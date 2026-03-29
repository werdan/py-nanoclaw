"""Agent dispatch: Claude Agent SDK ``query`` + session persistence."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    query,
)

from nanoclaw.models import Inbound
from nanoclaw.session import save_session_id

_DEFAULT_MODEL = "claude-sonnet-4-20250514"


def _model_name() -> str:
    return os.environ.get("CLAUDE_MODEL", _DEFAULT_MODEL)


def _claude_options(session_ref: list[str | None]) -> ClaudeAgentOptions:
    sid = session_ref[0]
    kwargs: dict[str, str] = {"model": _model_name()}
    if sid:
        kwargs["resume"] = sid
    return ClaudeAgentOptions(**kwargs)


async def dispatch(
    batch: list[Inbound],
    out_queue: asyncio.Queue[str],
    session_ref: list[str | None],
    session_path: Path,
) -> None:
    """
    Run one SDK ``query`` for the combined batch text; push the final text result to ``out_queue``.

    ``session_ref`` is a single-element list so the resumed session id can update after the
    first ``system/init`` (or result) message.
    """
    prompt = "\n".join(msg.content for msg in batch)

    async for message in query(
        prompt=prompt,
        options=_claude_options(session_ref),
    ):
        if isinstance(message, SystemMessage) and message.subtype == "init":
            d = message.data
            sid = d.get("session_id") or d.get("sessionId")
            if isinstance(sid, str) and sid:
                save_session_id(session_path, sid)
                session_ref[0] = sid

        elif isinstance(message, ResultMessage) and message.subtype == "success":
            # Persist session id from the result as well (covers SDKs that omit init shape).
            if message.session_id:
                save_session_id(session_path, message.session_id)
                session_ref[0] = message.session_id
            if message.result is not None:
                await out_queue.put(message.result)
