"""
Example wiring: stdin → inbound queue → worker → response queue → printer.

Run: ``python -m nanoclaw.cli`` (``python -u`` for fully line-buffered stdio.)

**Fast local agent testing (no docker agent rebuild):** load the same ``.env`` as compose, set
``NANOCLAW_AGENT_LOCAL=1``, point ``ONECLI_URL`` at the gateway on the host (e.g.
``http://127.0.0.1:10255`` while OneCLI is up), and set ``NANOCLAW_ONECLI_CA_PATH`` to a host
path for ``ca.pem`` (see LOCAL.md). The Telegram bot still uses Docker; this path is for
``nanoclaw`` CLI and quick iteration.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv

from nanoclaw.dispatch import dispatch as agent_dispatch, load_session_id
from nanoclaw.loop import run_worker_loop
from nanoclaw.models import Inbound

SESSION_PATH = Path.cwd() / ".nanoclaw_session"

async def reader(inbound: asyncio.Queue[Inbound], stop: asyncio.Event) -> None:
    """Read lines from stdin and enqueue non-empty messages."""
    print("Type messages; empty line does nothing. quit / exit / q or Ctrl-D to stop.")
    try:
        while not stop.is_set():
            line = await asyncio.to_thread(input, "> ")
            s = line.strip()
            if s.lower() in ("quit", "exit", "q"):
                break
            if s:
                await inbound.put(Inbound(s))
                await asyncio.sleep(0)
    except EOFError:
        pass
    finally:
        stop.set()


async def printer(out_queue: asyncio.Queue[str], stop: asyncio.Event) -> None:
    """Consumer: print replies from the agent."""
    while not stop.is_set():
        try:
            reply = await asyncio.wait_for(out_queue.get(), timeout=0.5)
        except TimeoutError:
            continue
        print(reply)


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
