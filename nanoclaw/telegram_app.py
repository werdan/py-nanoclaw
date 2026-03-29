"""
Telegram channel: incoming messages → ``inbound`` queue; ``out_queue`` → send to the same chat.

Uses the same ``run_worker_loop``, ``dispatch``, and session file as :mod:`nanoclaw.cli`.

Requires ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_USER_ID`` (your numeric Telegram **user** id — stable;
private-DM ``chat_id`` for ``send_message`` matches that id).

Run: ``python -m nanoclaw.telegram_app`` (``nanoclaw-telegram``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from nanoclaw.dispatch import dispatch as agent_dispatch
from nanoclaw.loop import run_worker_loop
from nanoclaw.models import Inbound
from nanoclaw.session import load_session_id

logger = logging.getLogger(__name__)

SESSION_PATH = Path.cwd() / ".nanoclaw_session"

# Transient network errors are common with Telegram; keep retrying with backoff.
_MAX_TRANSIENT_SEND_ATTEMPTS = 8


def _retry_after_seconds(exc: RetryAfter) -> float:
    ra = exc.retry_after
    if isinstance(ra, int):
        return float(ra)
    return float(ra.total_seconds())


async def send_telegram_message(
    bot: Bot,
    *,
    chat_id: int,
    text: str,
) -> None:
    """
    Send one message; do not raise. Retries flood-wait and transient network failures.

    Other Telegram errors are logged and the message is skipped (bad token, blocked bot, etc.).
    """
    network_attempt = 0
    while True:
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            return
        except RetryAfter as e:
            wait = _retry_after_seconds(e)
            logger.warning(
                "Telegram rate limit: retry after %.1f s (flood control; flaky networks are common).",
                wait,
            )
            await asyncio.sleep(wait)
        except (NetworkError, TimedOut) as e:
            network_attempt += 1
            if network_attempt > _MAX_TRANSIENT_SEND_ATTEMPTS:
                logger.exception(
                    "Telegram send failed after %s transient errors; giving up on this message: %s",
                    _MAX_TRANSIENT_SEND_ATTEMPTS,
                    e,
                )
                return
            delay = min(2.0 ** min(network_attempt, 6), 120.0)
            logger.warning(
                "Telegram network error (%s/%s): %s; retry in %.1fs",
                network_attempt,
                _MAX_TRANSIENT_SEND_ATTEMPTS,
                e,
                delay,
            )
            await asyncio.sleep(delay)
        except TelegramError as e:
            logger.exception("Telegram send failed (not retried): %s", e)
            return


def _required_user_id() -> int:
    raw = os.environ.get("TELEGRAM_USER_ID")
    if raw is None or str(raw).strip() == "":
        raise SystemExit(
            "Set TELEGRAM_USER_ID to your Telegram user id (integer). "
            "Only that user may use this bot. Find it via @userinfobot or https://t.me/userinfobot ."
        )
    return int(str(raw).strip())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in the environment or .env")

    allowed_user_id = _required_user_id()

    inbound: asyncio.Queue[Inbound] = asyncio.Queue()
    out_queue: asyncio.Queue[str] = asyncio.Queue()
    stop = asyncio.Event()
    session_ref: list[str | None] = [load_session_id(SESSION_PATH)]

    async def handle_batch(batch: list[Inbound]) -> None:
        await agent_dispatch(batch, out_queue, session_ref, SESSION_PATH)

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return
        user = update.effective_user
        if user is None:
            return
        if user.id != allowed_user_id:
            logger.warning("Rejected message from unauthorized user id %s", user.id)
            await update.message.reply_text("Unauthorized.")
            return
        await inbound.put(Inbound(update.message.text))

    async def send_outbound(application: Application) -> None:
        bot = application.bot
        while not stop.is_set():
            try:
                text = await asyncio.wait_for(out_queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            # Private DM: chat_id for send_message is the user's id (same as TELEGRAM_USER_ID).
            # TODO: Telegram caps messages at 4096 chars; split or truncate long Claude replies.
            await send_telegram_message(bot, chat_id=allowed_user_id, text=text)

    async def post_init(application: Application) -> None:
        asyncio.create_task(
            run_worker_loop(inbound, handle_batch, wait_timeout_s=0.5, stop=stop),
            name="nanoclaw-worker",
        )
        asyncio.create_task(send_outbound(application), name="nanoclaw-telegram-sender")

    async def post_stop(application: Application) -> None:
        stop.set()

    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & (~filters.COMMAND),
            on_text,
        )
    )

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
