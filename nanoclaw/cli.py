"""
Example wiring: stdin → inbound queue → worker → response queue → printer.

Run: ``python -m nanoclaw.cli`` (``python -u`` for fully line-buffered stdio.)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

from nanoclaw.dispatch import dispatch as agent_dispatch
from nanoclaw.loop import run_worker_loop
from nanoclaw.models import Inbound
from nanoclaw.session import load_session_id

SESSION_PATH = Path.cwd() / ".nanoclaw_session"


def _readline_with_prompt() -> str:
    """Prompt on stdout, read without embedding a prompt in ``input()`` (same stream as replies)."""
    sys.stdout.write("\n> ")
    sys.stdout.flush()
    return sys.stdin.readline()


def _emit_reply(reply: str) -> None:
    """Emit reply on stderr; flush stdout first so prompt and reply ordering is stable."""
    sys.stdout.flush()
    sys.stderr.write(f"\n{reply}\n")
    sys.stderr.flush()


async def reader(inbound: asyncio.Queue[Inbound], stop: asyncio.Event) -> None:
    """Blocking line read runs in a thread pool so the event loop stays responsive.

    If ``stop`` is set while a call is still blocked in the executor, that thread cannot be
    cancelled; it exits once the user submits a line or EOF, or when the process ends.
    """
    loop = asyncio.get_running_loop()
    print("Type messages; empty line does nothing. quit / exit / q or Ctrl-D to stop.")
    try:
        while not stop.is_set():
            line = await loop.run_in_executor(None, _readline_with_prompt)
            if line == "":
                break  # EOF (e.g. Ctrl-D on empty buffer)
            s = line.strip() if line else ""
            if s.lower() in ("quit", "exit", "q"):
                break
            if s:
                await inbound.put(Inbound(s))
                # Yield once so the event loop can schedule other tasks; does not guarantee reply
                # before the next prompt (worker → dispatch → printer are several hops away).
                await asyncio.sleep(0)
    finally:
        stop.set()


async def printer(out_queue: asyncio.Queue[str], stop: asyncio.Event) -> None:
    """Consumer: print replies from the agent (Telegram send, TUI, etc. would go here).

    Replies use **stderr** and a sync helper so they do not interleave with the **stdout** prompt
    (two writers on one stream would still race without a single UI thread).
    """
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        try:
            reply = await asyncio.wait_for(out_queue.get(), timeout=0.5)
        except TimeoutError:
            continue
        await loop.run_in_executor(None, _emit_reply, reply)


async def _run() -> None:
    inbound: asyncio.Queue[Inbound] = asyncio.Queue()
    out_queue: asyncio.Queue[str] = asyncio.Queue()
    stop = asyncio.Event()
    session_ref: list[str | None] = [load_session_id(SESSION_PATH)]

    async def handle_batch(batch: list[Inbound]) -> None:
        await agent_dispatch(batch, out_queue, session_ref, SESSION_PATH)

    await asyncio.gather(
        reader(inbound, stop),
        run_worker_loop(inbound, handle_batch, wait_timeout_s=0.5, stop=stop),
        printer(out_queue, stop),
    )


def main() -> None:
    load_dotenv()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
