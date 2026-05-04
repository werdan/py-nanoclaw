"""Tiny client for the credential broker sidecar (container/creds_broker/server.py).

Line-delimited JSON over a Unix domain socket, single round-trip per connection.
Used by:
- ``nanoclaw.google_auth`` (when ``NANOCLAW_AGENT_BROKER_SOCKET`` is set) to
  fetch short-lived Google OAuth access tokens for the agent's calendar / future
  Gmail tools, without ever holding a refresh token in this process.
- ``nanoclaw.telegram_app`` (when ``NANOCLAW_BOT_BROKER_SOCKET`` is set) to fetch
  the Telegram bot token at startup, so it doesn't have to live in `.env`.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AGENT_SOCKET_ENV = "NANOCLAW_AGENT_BROKER_SOCKET"
BOT_SOCKET_ENV = "NANOCLAW_BOT_BROKER_SOCKET"


class BrokerError(RuntimeError):
    """Raised when a broker call fails (transport error or `ok: false` response)."""


def _request(sock_path: str, payload: dict[str, Any], *, timeout_s: float = 10.0) -> dict[str, Any]:
    if not Path(sock_path).exists():
        raise BrokerError(f"broker socket not found: {sock_path}")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        s.connect(sock_path)
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        f = s.makefile("rb")
        line = f.readline()
    except (OSError, socket.timeout) as exc:
        raise BrokerError(f"broker transport error: {exc}") from exc
    finally:
        try:
            s.close()
        except OSError:
            pass

    if not line:
        raise BrokerError("broker returned empty response")
    try:
        resp = json.loads(line.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise BrokerError(f"broker returned non-JSON response: {exc}") from exc
    if not isinstance(resp, dict):
        raise BrokerError("broker response must be a JSON object")
    if not resp.get("ok"):
        raise BrokerError(f"broker error: {resp.get('error', '<no error message>')}")
    return resp


def is_agent_broker_available() -> bool:
    sock = os.environ.get(AGENT_SOCKET_ENV, "").strip()
    return bool(sock) and Path(sock).exists()


def is_bot_broker_available() -> bool:
    sock = os.environ.get(BOT_SOCKET_ENV, "").strip()
    return bool(sock) and Path(sock).exists()


def fetch_google_access_token(account: str, *, sock_path: str | None = None) -> dict[str, Any]:
    """Ask the broker for a short-lived Google OAuth access token for ``account``.
    Returns ``{"authorization": "Bearer ya29...", "expires_at": "..."}``."""
    sock = sock_path or os.environ.get(AGENT_SOCKET_ENV, "").strip()
    if not sock:
        raise BrokerError(f"{AGENT_SOCKET_ENV} not set")
    return _request(sock, {"op": "google_access_token", "account": account})


def fetch_telegram_bot_token(*, sock_path: str | None = None) -> str:
    """Ask the broker for the Telegram bot token. Returns the raw token string."""
    sock = sock_path or os.environ.get(BOT_SOCKET_ENV, "").strip()
    if not sock:
        raise BrokerError(f"{BOT_SOCKET_ENV} not set")
    resp = _request(sock, {"op": "telegram_bot_token"})
    token = resp.get("token")
    if not isinstance(token, str) or not token:
        raise BrokerError("broker returned empty/invalid telegram token")
    return token
