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
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import Bot, Update
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from nanoclaw.dispatch import dispatch as agent_dispatch, load_session_id
from nanoclaw.loop import run_worker_loop
from nanoclaw.models import Inbound

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


async def transcribe_telegram_voice(
    client: AsyncOpenAI,
    *,
    voice_file_id: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> str | None:
    """Download one Telegram voice message and transcribe it via OpenAI Whisper."""
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        tg_file = await context.bot.get_file(voice_file_id)
        await tg_file.download_to_drive(custom_path=str(tmp_path))
        with tmp_path.open("rb") as audio_file:
            result = await client.audio.transcriptions.create(model="whisper-1", file=audio_file)
        text = (result.text or "").strip()
        return text if text else None
    finally:
        tmp_path.unlink(missing_ok=True)


def cleanup_inbound_temp_files(batch: list[Inbound]) -> None:
    """Delete any per-message temp files attached to this batch."""
    for inbound in batch:
        for temp_path in inbound.temp_paths:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                logger.exception("Failed to delete temp file: %s", temp_path)


def _document_suffix(file_name: str | None, mime_type: str | None) -> str:
    if file_name:
        suffix = Path(file_name).suffix
        if suffix:
            return suffix
    if mime_type == "image/png":
        return ".png"
    if mime_type == "image/webp":
        return ".webp"
    return ".jpg"


def _is_image_document(mime_type: str | None) -> bool:
    return bool(mime_type and mime_type.startswith("image/"))


def _image_inbound(tmp_path: Path, caption: str) -> Inbound:
    content = f"User sent an image.\nLocal image path (temporary): {tmp_path}"
    if caption:
        content = f"User sent an image.\nCaption: {caption}\nLocal image path (temporary): {tmp_path}"
    return Inbound(content, temp_paths=(tmp_path,))


def _media_dir() -> Path:
    raw = os.environ.get("NANOCLAW_MEDIA_DIR")
    if raw and raw.strip():
        path = Path(raw).expanduser()
    else:
        path = Path.cwd() / ".nanoclaw_media"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _configure_logging() -> None:
    """Root WARNING so HTTP clients (httpx, httpcore) do not log full request URLs.

    Telegram embeds the bot token in the API path; those libraries log the full URL at INFO.
    ``nanoclaw`` stays at INFO for normal app messages.
    """
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("nanoclaw").setLevel(logging.INFO)


def main() -> None:
    _configure_logging()
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in the environment or .env")

    allowed_user_id = _required_user_id()
    openai_api_key = os.environ.get("OPENAI_API_KEY")
    openai_client = AsyncOpenAI(api_key=openai_api_key) if openai_api_key else None
    if openai_client is None:
        logger.warning("OPENAI_API_KEY is not set; voice transcription is disabled.")

    inbound: asyncio.Queue[Inbound] = asyncio.Queue()
    out_queue: asyncio.Queue[str] = asyncio.Queue()
    stop = asyncio.Event()
    session_ref: list[str | None] = [load_session_id(SESSION_PATH)]
    media_dir = _media_dir()
    # Set in post_init so handle_batch can notify on dispatch failure.
    bot_ref: list[Bot | None] = [None]

    async def handle_batch(batch: list[Inbound]) -> None:
        try:
            await agent_dispatch(batch, out_queue, session_ref, SESSION_PATH)
        except Exception:
            logger.exception("Agent dispatch failed")
            bot = bot_ref[0]
            if bot is not None:
                await send_telegram_message(
                    bot,
                    chat_id=allowed_user_id,
                    text="Sorry, processing failed. Please try again in a moment.",
                )
        finally:
            # Batch-level cleanup: once dispatch returns, the agent no longer needs uploaded image files.
            cleanup_inbound_temp_files(batch)

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return
        user = update.effective_user
        if user is None:
            return
        if user.id != allowed_user_id:
            logger.warning("Rejected message from unauthorized user id %s", user.id)
            return
        await inbound.put(Inbound(update.message.text))

    async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if openai_client is None or not update.message or not update.message.voice:
            return
        user = update.effective_user
        if user is None:
            return
        if user.id != allowed_user_id:
            logger.warning("Rejected voice from unauthorized user id %s", user.id)
            return
        try:
            text = await transcribe_telegram_voice(
                openai_client,
                voice_file_id=update.message.voice.file_id,
                context=context,
            )
        except Exception:
            logger.exception("Voice transcription failed.")
            await send_telegram_message(
                context.bot,
                chat_id=allowed_user_id,
                text="Voice transcription failed. Please try again.",
            )
            return
        if not text:
            logger.warning("Voice transcription returned empty text.")
            await send_telegram_message(
                context.bot,
                chat_id=allowed_user_id,
                text="I could not understand that voice message. Please try again.",
            )
            return
        await inbound.put(Inbound(text))

    async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.photo:
            return
        user = update.effective_user
        if user is None:
            return
        if user.id != allowed_user_id:
            logger.warning("Rejected photo from unauthorized user id %s", user.id)
            return

        photo = update.message.photo[-1]
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False, dir=media_dir) as tmp:
            tmp_path = Path(tmp.name)
        try:
            tg_file = await context.bot.get_file(photo.file_id)
            await tg_file.download_to_drive(custom_path=str(tmp_path))
        except Exception:
            tmp_path.unlink(missing_ok=True)
            logger.exception("Failed to download Telegram photo.")
            await send_telegram_message(
                context.bot,
                chat_id=allowed_user_id,
                text="I could not download that image. Please try again.",
            )
            return

        caption = (update.message.caption or "").strip()
        await inbound.put(_image_inbound(tmp_path, caption))

    async def on_image_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.document:
            return
        user = update.effective_user
        if user is None:
            return
        if user.id != allowed_user_id:
            logger.warning("Rejected document from unauthorized user id %s", user.id)
            return

        document = update.message.document
        if not _is_image_document(document.mime_type):
            # Ignore non-image documents for now.
            return

        suffix = _document_suffix(document.file_name, document.mime_type)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir=media_dir) as tmp:
            tmp_path = Path(tmp.name)
        try:
            tg_file = await context.bot.get_file(document.file_id)
            await tg_file.download_to_drive(custom_path=str(tmp_path))
        except Exception:
            tmp_path.unlink(missing_ok=True)
            logger.exception("Failed to download Telegram image document.")
            await send_telegram_message(
                context.bot,
                chat_id=allowed_user_id,
                text="I could not download that image file. Please try again.",
            )
            return

        caption = (update.message.caption or "").strip()
        await inbound.put(_image_inbound(tmp_path, caption))

    async def send_outbound(application: Application) -> None:
        bot = application.bot
        while not stop.is_set():
            try:
                text = await asyncio.wait_for(out_queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            # Private DM: chat_id for send_message is the user's id (same as TELEGRAM_USER_ID).
            for i in range(0, len(text), 4096):
                chunk = text[i : i + 4096]
                await send_telegram_message(bot, chat_id=allowed_user_id, text=chunk)

    async def post_init(application: Application) -> None:
        bot_ref[0] = application.bot
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
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.VOICE,
            on_voice,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.PHOTO,
            on_photo,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.Document.ALL,
            on_image_document,
        )
    )

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
