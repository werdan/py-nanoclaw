"""MCP server for Google Calendar across multiple accounts.

Every tool takes ``account`` as its first argument — one of the keys in
``nanoclaw.google_auth.ALLOWED_ACCOUNTS`` (``personal`` / ``work_admin`` / ``work_corp``).
Refresh tokens come from the JSON store managed by ``scripts/google_oauth_bootstrap.py``.

MVP scope is **read + create only**. Update / delete tools are intentionally not
exposed even though the underlying OAuth scope (``calendar.events``) would permit
them — keeping the tool surface narrow caps the agent's blast radius.

Scopes used:
    - calendar.calendarlist.readonly  → list_calendars
    - calendar.events                 → list_events, get_event, create_event
    - calendar.freebusy               → find_free_slots
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP

from nanoclaw.google_auth import ALLOWED_ACCOUNTS, list_accounts, load_credentials

mcp = FastMCP("nanoclaw-calendar")


def _service(account: str):
    """Build a Google Calendar service for the given account.

    Pulled out so tests can monkeypatch this single seam.
    """
    creds = load_credentials(account)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _summarize_event(ev: dict[str, Any]) -> dict[str, Any]:
    start = ev.get("start") or {}
    end = ev.get("end") or {}
    return {
        "id": ev.get("id"),
        "summary": ev.get("summary"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "location": ev.get("location"),
        "attendees": [a.get("email") for a in ev.get("attendees") or [] if a.get("email")],
        "html_link": ev.get("htmlLink"),
        "status": ev.get("status"),
    }


@mcp.tool()
def list_configured_accounts() -> list[str]:
    """List which Google accounts have stored credentials available."""
    return list_accounts()


@mcp.tool()
def list_calendars(account: str) -> list[dict[str, Any]]:
    """List subscribed calendars for ``account`` (one of personal/work_admin/work_corp).

    Returns id, summary, primary flag, and timezone for each calendar.
    """
    svc = _service(account)
    items = svc.calendarList().list().execute().get("items", []) or []
    return [
        {
            "id": it.get("id"),
            "summary": it.get("summary"),
            "primary": bool(it.get("primary")),
            "timezone": it.get("timeZone"),
            "access_role": it.get("accessRole"),
        }
        for it in items
    ]


@mcp.tool()
def list_events(
    account: str,
    time_min: str,
    time_max: str,
    calendar_id: str = "primary",
    q: str | None = None,
    max_results: int = 50,
) -> list[dict[str, Any]]:
    """List events on ``calendar_id`` between ``time_min`` and ``time_max``.

    ``time_min`` / ``time_max`` are RFC3339 timestamps (e.g. ``2026-05-04T00:00:00Z``).
    ``q`` is an optional free-text search filter. Recurring events are expanded.
    """
    svc = _service(account)
    kwargs: dict[str, Any] = {
        "calendarId": calendar_id,
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": max_results,
    }
    if q:
        kwargs["q"] = q
    resp = svc.events().list(**kwargs).execute()
    return [_summarize_event(ev) for ev in resp.get("items", []) or []]


@mcp.tool()
def get_event(account: str, event_id: str, calendar_id: str = "primary") -> dict[str, Any]:
    """Fetch the raw Google Calendar event payload for one event."""
    svc = _service(account)
    return svc.events().get(calendarId=calendar_id, eventId=event_id).execute()


@mcp.tool()
def create_event(
    account: str,
    summary: str,
    start: str,
    end: str,
    calendar_id: str = "primary",
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    """Create one event on ``calendar_id``.

    ``start`` and ``end`` are RFC3339 timestamps. If they're naive (no offset),
    pass ``timezone`` (e.g. ``Europe/Kiev``). ``attendees`` is a list of email
    addresses; invitation emails are sent automatically.
    """
    body: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start, **({"timeZone": timezone} if timezone else {})},
        "end": {"dateTime": end, **({"timeZone": timezone} if timezone else {})},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    svc = _service(account)
    return svc.events().insert(calendarId=calendar_id, body=body, sendUpdates="all").execute()


@mcp.tool()
def find_free_slots(
    accounts: list[str],
    time_min: str,
    time_max: str,
    duration_minutes: int,
) -> list[dict[str, str]]:
    """Find continuous free intervals of at least ``duration_minutes`` across all ``accounts``.

    Queries each account's primary calendar via the FreeBusy API, merges the
    busy intervals, and returns the gaps inside [time_min, time_max].
    Returned slots are in the same timezone as the input timestamps.
    """
    if duration_minutes <= 0:
        raise ValueError("duration_minutes must be > 0")
    for a in accounts:
        if a not in ALLOWED_ACCOUNTS:
            raise ValueError(f"unknown account {a!r}; allowed: {ALLOWED_ACCOUNTS}")

    busy: list[tuple[datetime, datetime]] = []
    for account in accounts:
        svc = _service(account)
        body = {"timeMin": time_min, "timeMax": time_max, "items": [{"id": "primary"}]}
        resp = svc.freebusy().query(body=body).execute()
        cal = (resp.get("calendars") or {}).get("primary") or {}
        for b in cal.get("busy") or []:
            busy.append(
                (
                    datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                    datetime.fromisoformat(b["end"].replace("Z", "+00:00")),
                )
            )

    busy.sort()
    merged: list[list[datetime]] = []
    for s, e in busy:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    window_start = datetime.fromisoformat(time_min.replace("Z", "+00:00"))
    window_end = datetime.fromisoformat(time_max.replace("Z", "+00:00"))
    free: list[dict[str, str]] = []
    cursor = window_start
    for s, e in merged:
        if s > cursor and (s - cursor).total_seconds() / 60 >= duration_minutes:
            free.append({"start": cursor.isoformat(), "end": s.isoformat()})
        if e > cursor:
            cursor = e
    if window_end > cursor and (window_end - cursor).total_seconds() / 60 >= duration_minutes:
        free.append({"start": cursor.isoformat(), "end": window_end.isoformat()})
    return free


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
