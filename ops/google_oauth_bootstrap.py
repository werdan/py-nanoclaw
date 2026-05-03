"""Local one-shot OAuth bootstrap — obtain a refresh token for one Google account
and store it in the shared credential file consumed by ``nanoclaw.calendar_mcp``.

Run on your laptop (needs a browser). Run once per account.

Usage:

    .venv/bin/python ops/google_oauth_bootstrap.py \\
        --account personal \\
        --client-secrets client_secret_*.json \\
        --creds runtime/sessions/.nanoclaw_google_creds.json

Then ``scp`` the resulting creds file to the VM at ``~/nanoclaw/runtime/sessions/``.

Scopes requested are the minimum to cover MVP read+create:

    calendar.calendarlist.readonly  — list_calendars
    calendar.events                 — list_events / get_event / create_event
    calendar.freebusy               — find_free_slots

(``calendar.events`` is the narrowest scope that allows event creation; Google has
no separate "create-only" scope. The MCP layer in ``nanoclaw/calendar_mcp.py``
deliberately doesn't expose update/delete tools to keep the agent's blast radius
small.)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from nanoclaw.google_auth import ALLOWED_ACCOUNTS, upsert_account

SCOPES = [
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
]


def _read_client_secrets(path: Path) -> tuple[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    section = raw.get("installed") or raw.get("web") or {}
    cid = section.get("client_id")
    csec = section.get("client_secret")
    if not cid or not csec:
        sys.exit(f"client_secrets file at {path} missing client_id / client_secret")
    return cid, csec


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, choices=list(ALLOWED_ACCOUNTS))
    parser.add_argument("--client-secrets", required=True, type=Path,
                        help="path to the Desktop OAuth client JSON downloaded from GCP")
    parser.add_argument("--creds", type=Path, default=None,
                        help="output creds file (defaults to $NANOCLAW_GOOGLE_CREDS_PATH or ./.nanoclaw_google_creds.json)")
    args = parser.parse_args(argv)

    secrets_path: Path = args.client_secrets.expanduser().resolve()
    if not secrets_path.is_file():
        sys.exit(f"client secrets file not found: {secrets_path}")
    client_id, client_secret = _read_client_secrets(secrets_path)

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", open_browser=True)

    if not creds.refresh_token:
        sys.exit(
            "Google did not return a refresh_token. Re-authorize from scratch — "
            "if you've already authorized this client for this account, revoke it at "
            "https://myaccount.google.com/permissions and rerun."
        )

    # Probe identity so we can store an `email` hint with the credentials.
    email: str | None = None
    try:
        cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
        calendar = cal.calendarList().list(maxResults=250).execute()
        for item in calendar.get("items", []) or []:
            if item.get("primary"):
                email = item.get("id")
                break
    except Exception as exc:  # pragma: no cover - best-effort identity probe
        print(f"warning: could not probe primary calendar email: {exc}", file=sys.stderr)

    upsert_account(
        args.account,
        refresh_token=creds.refresh_token,
        email=email,
        client_id=client_id,
        client_secret=client_secret,
        scopes=list(creds.scopes or SCOPES),
        path=args.creds,
    )
    print(f"\n✓ stored refresh token for account={args.account!r}"
          + (f" (email: {email})" if email else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
