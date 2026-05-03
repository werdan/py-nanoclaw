"""Smoke-test read access across every configured Google account.

For each account in the credential store, runs:
  1. list_calendars  (calendar.calendarlist.readonly scope)
  2. list_events on the primary calendar for the next 7 days  (calendar.events scope)
  3. freebusy.query on the primary calendar for the next 24 hours  (calendar.freebusy scope)

Prints PASS / FAIL per account-step and exits non-zero if any check fails.
This is a manual diagnostic — not a unit test.

Usage:

    .venv/bin/python ops/smoke_read_all_accounts.py \\
        --creds runtime/sessions/.nanoclaw_google_creds.json
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanoclaw.calendar_mcp import find_free_slots, list_calendars, list_events
from nanoclaw.google_auth import list_accounts


def _check(label: str, fn) -> tuple[bool, str]:
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 — diagnostic
        return False, f"{type(exc).__name__}: {exc}"
    if isinstance(result, list):
        return True, f"ok ({len(result)} item{'s' if len(result) != 1 else ''})"
    return True, "ok"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--creds",
        type=Path,
        default=Path("runtime/sessions/.nanoclaw_google_creds.json"),
        help="path to the credential JSON",
    )
    args = parser.parse_args(argv)

    # nanoclaw.google_auth uses NANOCLAW_GOOGLE_CREDS_PATH or cwd default; honor --creds.
    import os
    os.environ["NANOCLAW_GOOGLE_CREDS_PATH"] = str(args.creds.resolve())

    accounts = list_accounts(path=args.creds)
    if not accounts:
        print(f"no accounts in {args.creds}", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    week = now + timedelta(days=7)
    day = now + timedelta(hours=24)

    rfc = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731

    overall_ok = True
    for account in accounts:
        print(f"\n=== {account} ===")
        steps = [
            ("list_calendars", lambda a=account: list_calendars(a)),
            (
                "list_events (next 7d, primary)",
                lambda a=account: list_events(a, time_min=rfc(now), time_max=rfc(week), max_results=5),
            ),
            (
                "find_free_slots (next 24h, ≥30min)",
                lambda a=account: find_free_slots(
                    [a], time_min=rfc(now), time_max=rfc(day), duration_minutes=30
                ),
            ),
        ]
        for label, fn in steps:
            ok, msg = _check(label, fn)
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] {label}: {msg}")
            if not ok:
                overall_ok = False
                # print one-line traceback for fast diagnosis
                traceback.print_exc(limit=1)

    print()
    if overall_ok:
        print("✓ all read smoke checks passed")
        return 0
    print("✗ at least one read smoke check failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
