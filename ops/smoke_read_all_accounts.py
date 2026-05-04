"""Smoke-test calendar reads end-to-end.

Calls each cross-account read tool once and prints a one-line PASS/FAIL
per tool, plus a per-account count breakdown. Exits non-zero if any tool
returns a hard error (the new tools surface per-account failures as soft
errors inside the response — those count as a partial PASS but are
flagged in the output).

Usage:

    .venv/bin/python ops/smoke_read_all_accounts.py \\
        --creds runtime/sessions/.nanoclaw_google_creds.json
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import Counter

from nanoclaw.calendar_mcp import find_free_slots, list_calendars, list_events
from nanoclaw.google_auth import list_accounts


def _fmt_per_account(items: list[dict], key: str = "account") -> str:
    if not items:
        return "(empty)"
    return ", ".join(f"{a}={n}" for a, n in sorted(Counter(i.get(key) for i in items).items()))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--creds",
        type=Path,
        default=Path("runtime/sessions/.nanoclaw_google_creds.json"),
        help="path to the credential JSON (used only when no broker is configured)",
    )
    args = parser.parse_args(argv)
    os.environ.setdefault("NANOCLAW_GOOGLE_CREDS_PATH", str(args.creds.resolve()))

    accounts = list_accounts(path=args.creds)
    if not accounts:
        print("no accounts configured", file=sys.stderr)
        return 2

    print(f"configured accounts: {accounts}")
    print()

    now = datetime.now(timezone.utc)
    week = now + timedelta(days=7)
    day = now + timedelta(hours=24)
    rfc = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731

    overall_ok = True

    print("[1/3] list_calendars (cross-account)")
    try:
        r = list_calendars()
        print(f"  PASS: {len(r['calendars'])} calendars total — {_fmt_per_account(r['calendars'])}")
        for e in r["errors"]:
            print(f"  WARN: account={e['account']} error={e['error']}")
            overall_ok = False
    except Exception as exc:  # noqa: BLE001
        overall_ok = False
        print(f"  FAIL: {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=1)

    print()
    print("[2/3] list_events (cross-account, next 7d)")
    try:
        r = list_events(time_min=rfc(now), time_max=rfc(week), max_results_per_account=5)
        print(f"  PASS: {len(r['events'])} events — {_fmt_per_account(r['events'])}")
        for e in r["errors"]:
            print(f"  WARN: account={e['account']} error={e['error']}")
            overall_ok = False
    except Exception as exc:  # noqa: BLE001
        overall_ok = False
        print(f"  FAIL: {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=1)

    print()
    print("[3/3] find_free_slots (next 24h, ≥30min)")
    try:
        slots = find_free_slots(accounts=accounts, time_min=rfc(now), time_max=rfc(day), duration_minutes=30)
        print(f"  PASS: {len(slots)} free slot(s)")
    except Exception as exc:  # noqa: BLE001
        overall_ok = False
        print(f"  FAIL: {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=1)

    print()
    print("✓ all read smoke checks passed" if overall_ok else "✗ at least one read smoke check failed")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
