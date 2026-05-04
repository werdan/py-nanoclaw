"""Build the bot's ``AsyncOpenAI`` client with OneCLI as an HTTPS injector.

Tier 1.1 follow-up: keeps `OPENAI_API_KEY` out of the bot's env entirely.
The bot sends a placeholder ``Authorization: Bearer placeholder`` header to
``api.openai.com``. OneCLI's host-pattern secret for that hostname swaps the
header in flight with the real key. The bot process never holds the value.

Why not just set HTTPS_PROXY globally on the bot:
    The bot also calls ``api.telegram.org`` for the Telegram client, and
    OneCLI doesn't have a rule for that host. Routing every outbound call
    through OneCLI broke startup. So we attach the proxy to the OpenAI
    httpx client *only* — Telegram traffic stays direct.

Fallback chain:
    1. If ONECLI_AGENT_TOKEN + ONECLI_URL + CA cert path are all present,
       build a proxied client with placeholder API key.
    2. Otherwise, if OPENAI_API_KEY is set in env, build a direct client.
    3. Otherwise, return None — caller disables voice transcription.
"""

from __future__ import annotations

import logging
import os
import ssl
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_DEFAULT_CA_PATH = "/onecli-data/gateway/ca.pem"
_PLACEHOLDER_KEY = "placeholder"


def _try_build_proxy_client(ca_path: str) -> AsyncOpenAI | None:
    """Attempt to build the OneCLI-proxied client. Returns None if any
    precondition fails (env not set, CA missing, CA unparseable)."""
    onecli_token = os.environ.get("ONECLI_AGENT_TOKEN", "").strip()
    onecli_url = os.environ.get("ONECLI_URL", "").strip()
    if not onecli_token or not onecli_url:
        return None
    if not Path(ca_path).is_file():
        return None
    try:
        ssl_ctx = ssl.create_default_context(cafile=ca_path)
    except (ssl.SSLError, OSError) as exc:
        logger.warning("OneCLI CA cert at %s could not be loaded (%s)", ca_path, exc)
        return None

    parsed = urlparse(onecli_url)
    netloc = parsed.netloc or "onecli:10255"
    proxy = f"http://x:{quote(onecli_token, safe='')}@{netloc}"
    http_client = httpx.AsyncClient(proxy=proxy, verify=ssl_ctx, timeout=30.0)
    logger.info(
        "OpenAI client routed through OneCLI proxy at %s (real key injected mid-stream)",
        netloc,
    )
    return AsyncOpenAI(api_key=_PLACEHOLDER_KEY, http_client=http_client)


def build_async_openai_client(*, ca_path: str = _DEFAULT_CA_PATH) -> AsyncOpenAI | None:
    """Construct the AsyncOpenAI client. Tries the OneCLI proxy first, falls
    back to a direct ``OPENAI_API_KEY`` if proxy isn't usable. Returns None
    if no auth path is available (caller disables voice transcription)."""
    proxied = _try_build_proxy_client(ca_path)
    if proxied is not None:
        return proxied

    direct_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if direct_key:
        logger.info("OpenAI client using direct OPENAI_API_KEY from env")
        return AsyncOpenAI(api_key=direct_key)

    logger.warning(
        "no OneCLI proxy and no OPENAI_API_KEY in env — voice transcription will be disabled"
    )
    return None
