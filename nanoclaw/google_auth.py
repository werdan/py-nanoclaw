"""Per-account Google OAuth credential storage and loading.

Credentials live in a single JSON file (default: ``runtime/sessions/.nanoclaw_google_creds.json``)
shared by every account, with a single OAuth client and one refresh token per account:

    {
      "client":   { "client_id": "...", "client_secret": "...", "token_uri": "..." },
      "scopes":   ["..."],
      "accounts": { "<account>": { "refresh_token": "...", "email": "..." } }
    }

Allowed account keys are restricted to a known set so the agent can't reach into arbitrary
slots, and so a typo in the agent's tool call surfaces as a clear error.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials

PATH_ENV = "NANOCLAW_GOOGLE_CREDS_PATH"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"

ALLOWED_ACCOUNTS: tuple[str, ...] = ("personal", "work_admin", "work_corp")


class CredsError(RuntimeError):
    """Raised for any user-facing credential storage or lookup failure."""


def creds_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    raw = os.environ.get(PATH_ENV, "").strip()
    if raw:
        return Path(raw)
    return Path.cwd() / ".nanoclaw_google_creds.json"


def _load_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise CredsError(f"google creds file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CredsError(f"google creds file is not valid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CredsError(f"google creds file root must be a JSON object: {path}")
    return data


def _save_store(store: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def list_accounts(path: Path | str | None = None) -> list[str]:
    p = creds_path(path)
    if not p.exists():
        return []
    try:
        store = _load_store(p)
    except CredsError:
        return []
    accounts = store.get("accounts", {})
    if not isinstance(accounts, dict):
        return []
    return sorted(k for k in accounts if k in ALLOWED_ACCOUNTS)


def _broker_refresh_handler(account: str):
    """Returns a `refresh_handler` callable that asks the broker sidecar for a
    fresh access token. Used so a running ``Credentials`` object that hits a 401
    mid-session re-mints transparently — same UX as a refresh-token-backed
    Credentials, except the refresh token never leaves the broker container.
    """
    from datetime import datetime, timedelta
    from nanoclaw.creds_broker_client import BrokerError, fetch_google_access_token

    def _handler(_request, _scopes):
        try:
            resp = fetch_google_access_token(account)
        except BrokerError as exc:
            raise CredsError(f"broker access-token fetch failed for {account!r}: {exc}") from exc
        auth = resp["authorization"]
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else auth
        # Google access tokens are typically valid 60 minutes. The broker tells
        # us the actual expiry; if it didn't, assume 50 min for a safety margin.
        expires_raw = resp.get("expires_at")
        if isinstance(expires_raw, str):
            try:
                expiry = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
                if expiry.tzinfo is not None:
                    expiry = expiry.replace(tzinfo=None)  # google-auth uses naive UTC
            except ValueError:
                expiry = datetime.utcnow() + timedelta(minutes=50)
        else:
            expiry = datetime.utcnow() + timedelta(minutes=50)
        return token, expiry

    return _handler


def _broker_credentials(account: str) -> Credentials:
    """Build a Credentials backed by the broker — no refresh_token in this process.

    The handler is what googleapiclient invokes whenever it sees an expired
    token; we pre-mint once at construction so the first API call doesn't pay
    the broker round-trip latency.
    """
    handler = _broker_refresh_handler(account)
    token, expiry = handler(None, None)
    return Credentials(token=token, refresh_handler=handler, expiry=expiry)


def load_credentials(account: str, path: Path | str | None = None) -> Credentials:
    if account not in ALLOWED_ACCOUNTS:
        raise CredsError(f"unknown account {account!r}; allowed: {ALLOWED_ACCOUNTS}")

    # Production path: ask the broker sidecar; refresh tokens never reach this
    # process. Activated when the broker socket env var is set AND the socket
    # exists on disk (so a misconfigured mount falls back instead of hanging).
    from nanoclaw.creds_broker_client import is_agent_broker_available
    if is_agent_broker_available():
        return _broker_credentials(account)

    # Fallback path: read the local creds file directly. Used in local dev
    # (no sidecar) and as a graceful degradation if the broker is unreachable
    # at startup.
    store = _load_store(creds_path(path))
    accounts = store.get("accounts", {})
    if not isinstance(accounts, dict) or account not in accounts:
        raise CredsError(
            f"account {account!r} not configured — "
            f"run `python scripts/google_oauth_bootstrap.py --account {account} ...`"
        )
    acc = accounts[account]
    refresh_token = acc.get("refresh_token") if isinstance(acc, dict) else None
    if not refresh_token:
        raise CredsError(f"account {account!r} has no refresh_token stored")
    client = store.get("client") or {}
    if not client.get("client_id") or not client.get("client_secret"):
        raise CredsError("creds file is missing client.client_id / client.client_secret")
    scopes = store.get("scopes") or []
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=client.get("token_uri") or DEFAULT_TOKEN_URI,
        client_id=client["client_id"],
        client_secret=client["client_secret"],
        scopes=list(scopes),
    )


def upsert_account(
    account: str,
    *,
    refresh_token: str,
    email: str | None,
    client_id: str,
    client_secret: str,
    scopes: list[str],
    token_uri: str = DEFAULT_TOKEN_URI,
    path: Path | str | None = None,
) -> None:
    """Add or replace one account in the credential store, merging scopes/client info."""
    if account not in ALLOWED_ACCOUNTS:
        raise CredsError(f"unknown account {account!r}; allowed: {ALLOWED_ACCOUNTS}")
    if not refresh_token:
        raise CredsError("refresh_token is required")
    p = creds_path(path)
    try:
        store = _load_store(p) if p.exists() else {}
    except CredsError:
        store = {}

    client = store.get("client") if isinstance(store.get("client"), dict) else {}
    client.update(
        {"client_id": client_id, "client_secret": client_secret, "token_uri": token_uri}
    )
    store["client"] = client

    existing_scopes = set(store.get("scopes") or [])
    store["scopes"] = sorted(existing_scopes | set(scopes))

    accounts = store.get("accounts") if isinstance(store.get("accounts"), dict) else {}
    accounts[account] = {"refresh_token": refresh_token, "email": email}
    store["accounts"] = accounts

    _save_store(store, p)
