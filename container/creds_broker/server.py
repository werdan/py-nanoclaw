"""Credential broker sidecar.

Vends short-lived secrets to the bot and the agent over per-consumer Unix
sockets. The high-value secrets (Google OAuth refresh tokens, Telegram bot
token) live only inside this container and never reach the consumer's
filesystem.

Two separate UDS listeners on disjoint paths:

  /run/agent-creds/sock   →  ops accepted: ``google_access_token``
  /run/bot-creds/sock     →  ops accepted: ``telegram_bot_token``

Each socket is mounted into exactly one consumer container, so socket
isolation == per-consumer authz. No SO_PEERCRED or shared-socket multiplexing
needed.

Wire protocol: line-delimited JSON, single round-trip per connection.

  Request:  {"op": "google_access_token", "account": "personal"}\\n
  Response: {"ok": true, "authorization": "Bearer ya29...", "expires_at": "..."}\\n

  Request:  {"op": "telegram_bot_token"}\\n
  Response: {"ok": true, "token": "8360..."}\\n

Errors:    {"ok": false, "error": "..."}

Secret files live under ``BROKER_SECRETS_DIR`` (default ``/secrets``):
  - ``google-oauth-creds.json`` — same shape as nanoclaw.google_auth's store
  - ``telegram-bot-token``       — single-line file, mode 0600
"""

from __future__ import annotations

import json
import logging
import os
import socket
import socketserver
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

ALLOWED_ACCOUNTS: tuple[str, ...] = ("personal", "work_admin", "work_corp")
_DEFAULT_SECRETS_DIR = "/secrets"
_DEFAULT_AGENT_SOCKET = "/run/agent-creds/sock"
_DEFAULT_BOT_SOCKET = "/run/bot-creds/sock"


def _load_google_store(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"google creds file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"google creds file root must be a JSON object: {path}")
    return data


def _refresh_google_access_token(store: dict[str, Any], account: str) -> dict[str, Any]:
    if account not in ALLOWED_ACCOUNTS:
        return {"ok": False, "error": f"unknown account {account!r}"}
    accounts = store.get("accounts") or {}
    if account not in accounts:
        return {"ok": False, "error": f"account {account!r} not configured"}
    acc = accounts[account]
    client = store.get("client") or {}
    if not client.get("client_id") or not client.get("client_secret"):
        return {"ok": False, "error": "creds file missing client_id / client_secret"}
    if not acc.get("refresh_token"):
        return {"ok": False, "error": f"account {account!r} missing refresh_token"}

    creds = Credentials(
        token=None,
        refresh_token=acc["refresh_token"],
        token_uri=client.get("token_uri") or "https://oauth2.googleapis.com/token",
        client_id=client["client_id"],
        client_secret=client["client_secret"],
        scopes=list(store.get("scopes") or []),
    )
    creds.refresh(Request())
    expiry = creds.expiry
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return {
        "ok": True,
        "authorization": f"Bearer {creds.token}",
        "expires_at": expiry.isoformat() if expiry else None,
    }


def _read_telegram_token(secrets_dir: Path) -> dict[str, Any]:
    p = secrets_dir / "telegram-bot-token"
    if not p.is_file():
        return {"ok": False, "error": f"telegram-bot-token file not at {p}"}
    token = p.read_text(encoding="utf-8").strip()
    if not token:
        return {"ok": False, "error": "telegram-bot-token file is empty"}
    return {"ok": True, "token": token}


def _agent_dispatch(secrets_dir: Path, req: dict[str, Any]) -> dict[str, Any]:
    op = req.get("op")
    if op == "google_access_token":
        account = req.get("account")
        if not isinstance(account, str):
            return {"ok": False, "error": "missing or non-string 'account'"}
        # Reload the store on every call so refresh-token rotations land
        # without restarting the broker.
        try:
            store = _load_google_store(secrets_dir / "google-oauth-creds.json")
        except (FileNotFoundError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return _refresh_google_access_token(store, account)
    return {"ok": False, "error": f"unsupported op {op!r} on agent socket"}


def _bot_dispatch(secrets_dir: Path, req: dict[str, Any]) -> dict[str, Any]:
    op = req.get("op")
    if op == "telegram_bot_token":
        return _read_telegram_token(secrets_dir)
    return {"ok": False, "error": f"unsupported op {op!r} on bot socket"}


def _make_handler(dispatch: Callable[[dict[str, Any]], dict[str, Any]]):
    class _Handler(socketserver.StreamRequestHandler):
        timeout = 5

        def handle(self) -> None:
            try:
                line = self.rfile.readline()
                if not line:
                    return
                req = json.loads(line.decode("utf-8"))
                if not isinstance(req, dict):
                    resp: dict[str, Any] = {"ok": False, "error": "request must be a JSON object"}
                else:
                    resp = dispatch(req)
            except json.JSONDecodeError as exc:
                resp = {"ok": False, "error": f"json decode: {exc}"}
            except Exception as exc:  # noqa: BLE001 - last-resort wrapper
                logger.exception("broker handler error")
                resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            try:
                self.wfile.write((json.dumps(resp) + "\n").encode("utf-8"))
            except OSError:
                pass

    return _Handler


class _ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def _serve(sock_path: str, dispatch: Callable[[dict[str, Any]], dict[str, Any]]) -> threading.Thread:
    p = Path(sock_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    server = _ThreadedUnixServer(sock_path, _make_handler(dispatch))
    # 0o660: broker UID (owner) + group readable. The mounted-into-consumer
    # container provides the actual access boundary; mode is belt-and-suspenders.
    os.chmod(sock_path, 0o660)
    t = threading.Thread(target=server.serve_forever, daemon=True, name=f"broker-{p.name}")
    t.start()
    logger.info("broker listening on %s", sock_path)
    return t


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    secrets_dir = Path(os.environ.get("BROKER_SECRETS_DIR", _DEFAULT_SECRETS_DIR))
    if not secrets_dir.is_dir():
        logger.error("BROKER_SECRETS_DIR=%s is not a directory; refusing to start", secrets_dir)
        return 2

    agent_sock = os.environ.get("BROKER_AGENT_SOCKET", _DEFAULT_AGENT_SOCKET)
    bot_sock = os.environ.get("BROKER_BOT_SOCKET", _DEFAULT_BOT_SOCKET)

    threads = [
        _serve(agent_sock, lambda req: _agent_dispatch(secrets_dir, req)),
        _serve(bot_sock, lambda req: _bot_dispatch(secrets_dir, req)),
    ]
    for t in threads:
        t.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
