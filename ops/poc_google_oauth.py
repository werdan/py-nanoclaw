"""POC — verify a Google account can authorize against our Desktop OAuth client.

Usage (run on your laptop, not the VM):

    .venv/bin/python ops/poc_google_oauth.py path/to/client_secret.json

A browser tab opens. Sign in with the account under test. The script then
makes one read-only events.list call against the primary calendar and prints
the first event (or "no events").

The point of this script is **only** to answer: "does Google let this account
authorize this OAuth client?" — especially for the @vaimo.com account, which
may be blocked by the corporate Workspace admin's third-party-app policy.

Refresh tokens obtained here are NOT stored. This is a throwaway probe.
"""

from __future__ import annotations

import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Narrowest scope that exercises calendar permission. If even this is blocked,
# wider scopes will be too — so we don't need to test write scopes here.
SCOPES = ["https://www.googleapis.com/auth/calendar.events.readonly"]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <client_secret.json>", file=sys.stderr)
        return 2

    secrets_path = Path(argv[1]).expanduser().resolve()
    if not secrets_path.is_file():
        print(f"client secrets file not found: {secrets_path}", file=sys.stderr)
        return 2

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", open_browser=True)

    print(f"\n✓ authorization succeeded for scopes: {creds.scopes}")
    print(f"  refresh_token present: {bool(creds.refresh_token)}")

    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    resp = service.events().list(calendarId="primary", maxResults=1).execute()
    items = resp.get("items", [])
    if not items:
        print("  primary calendar accessible — no upcoming events found")
    else:
        ev = items[0]
        print(f"  primary calendar accessible — first event: {ev.get('summary')!r} ({ev.get('start')})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
