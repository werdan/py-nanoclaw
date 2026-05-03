from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import nanoclaw.calendar_mcp as cm


def _make_service(events_return=None, calendars_return=None, freebusy_return=None,
                  insert_return=None, get_return=None):
    svc = MagicMock()
    if events_return is not None:
        svc.events.return_value.list.return_value.execute.return_value = events_return
    if calendars_return is not None:
        svc.calendarList.return_value.list.return_value.execute.return_value = calendars_return
    if freebusy_return is not None:
        svc.freebusy.return_value.query.return_value.execute.return_value = freebusy_return
    if insert_return is not None:
        svc.events.return_value.insert.return_value.execute.return_value = insert_return
    if get_return is not None:
        svc.events.return_value.get.return_value.execute.return_value = get_return
    return svc


def test_list_calendars_summarizes_response(monkeypatch) -> None:
    svc = _make_service(calendars_return={
        "items": [
            {"id": "primary", "summary": "Me", "primary": True, "timeZone": "Europe/Kiev", "accessRole": "owner"},
            {"id": "team@example.com", "summary": "Team", "timeZone": "UTC", "accessRole": "reader"},
        ]
    })
    monkeypatch.setattr(cm, "_service", lambda account: svc)

    result = cm.list_calendars("personal")

    assert result == [
        {"id": "primary", "summary": "Me", "primary": True, "timezone": "Europe/Kiev", "access_role": "owner"},
        {"id": "team@example.com", "summary": "Team", "primary": False, "timezone": "UTC", "access_role": "reader"},
    ]


def test_list_events_passes_query_params(monkeypatch) -> None:
    svc = _make_service(events_return={"items": []})
    monkeypatch.setattr(cm, "_service", lambda account: svc)

    cm.list_events(
        "personal",
        time_min="2026-05-04T00:00:00Z",
        time_max="2026-05-05T00:00:00Z",
        q="standup",
        max_results=10,
    )

    svc.events.return_value.list.assert_called_once_with(
        calendarId="primary",
        timeMin="2026-05-04T00:00:00Z",
        timeMax="2026-05-05T00:00:00Z",
        singleEvents=True,
        orderBy="startTime",
        maxResults=10,
        q="standup",
    )


def test_list_events_summarizes_items(monkeypatch) -> None:
    svc = _make_service(events_return={
        "items": [
            {
                "id": "abc",
                "summary": "Standup",
                "start": {"dateTime": "2026-05-04T09:00:00+02:00"},
                "end": {"dateTime": "2026-05-04T09:15:00+02:00"},
                "location": "Zoom",
                "attendees": [{"email": "a@x.com"}, {"email": "b@x.com"}, {}],
                "htmlLink": "https://...",
                "status": "confirmed",
            },
            {
                "id": "all-day",
                "summary": "Holiday",
                "start": {"date": "2026-05-09"},
                "end": {"date": "2026-05-10"},
            },
        ]
    })
    monkeypatch.setattr(cm, "_service", lambda account: svc)

    result = cm.list_events(
        "personal", time_min="2026-05-04T00:00:00Z", time_max="2026-05-11T00:00:00Z",
    )

    assert result[0]["id"] == "abc"
    assert result[0]["start"] == "2026-05-04T09:00:00+02:00"
    assert result[0]["attendees"] == ["a@x.com", "b@x.com"]
    assert result[1]["start"] == "2026-05-09"  # all-day fallback
    assert result[1]["end"] == "2026-05-10"


def test_list_events_omits_q_when_none(monkeypatch) -> None:
    svc = _make_service(events_return={"items": []})
    monkeypatch.setattr(cm, "_service", lambda account: svc)

    cm.list_events("personal", time_min="2026-05-04T00:00:00Z", time_max="2026-05-05T00:00:00Z")

    kwargs = svc.events.return_value.list.call_args.kwargs
    assert "q" not in kwargs


def test_create_event_builds_minimal_body(monkeypatch) -> None:
    svc = _make_service(insert_return={"id": "new", "htmlLink": "https://..."})
    monkeypatch.setattr(cm, "_service", lambda account: svc)

    result = cm.create_event(
        "personal",
        summary="Lunch",
        start="2026-05-04T12:00:00+02:00",
        end="2026-05-04T13:00:00+02:00",
    )

    svc.events.return_value.insert.assert_called_once()
    kwargs = svc.events.return_value.insert.call_args.kwargs
    assert kwargs["calendarId"] == "primary"
    assert kwargs["sendUpdates"] == "all"
    body = kwargs["body"]
    assert body["summary"] == "Lunch"
    assert body["start"] == {"dateTime": "2026-05-04T12:00:00+02:00"}
    assert body["end"] == {"dateTime": "2026-05-04T13:00:00+02:00"}
    assert "description" not in body
    assert "attendees" not in body
    assert result == {"id": "new", "htmlLink": "https://..."}


def test_create_event_full_body_with_attendees_and_timezone(monkeypatch) -> None:
    svc = _make_service(insert_return={"id": "new"})
    monkeypatch.setattr(cm, "_service", lambda account: svc)

    cm.create_event(
        "work_admin",
        summary="Sync",
        start="2026-05-04T15:00:00",
        end="2026-05-04T16:00:00",
        calendar_id="team@example.com",
        description="quarterly review",
        location="HQ",
        attendees=["a@x.com", "b@x.com"],
        timezone="Europe/Kiev",
    )

    body = svc.events.return_value.insert.call_args.kwargs["body"]
    assert body["start"] == {"dateTime": "2026-05-04T15:00:00", "timeZone": "Europe/Kiev"}
    assert body["end"] == {"dateTime": "2026-05-04T16:00:00", "timeZone": "Europe/Kiev"}
    assert body["description"] == "quarterly review"
    assert body["location"] == "HQ"
    assert body["attendees"] == [{"email": "a@x.com"}, {"email": "b@x.com"}]
    assert svc.events.return_value.insert.call_args.kwargs["calendarId"] == "team@example.com"


def test_get_event_returns_raw(monkeypatch) -> None:
    raw = {"id": "abc", "summary": "X", "extendedProperties": {"private": {"k": "v"}}}
    svc = _make_service(get_return=raw)
    monkeypatch.setattr(cm, "_service", lambda account: svc)

    assert cm.get_event("personal", "abc") == raw
    svc.events.return_value.get.assert_called_once_with(calendarId="primary", eventId="abc")


def test_find_free_slots_validates_unknown_account() -> None:
    with pytest.raises(ValueError, match="unknown account"):
        cm.find_free_slots(
            ["bogus"], time_min="2026-05-04T09:00:00+00:00",
            time_max="2026-05-04T17:00:00+00:00", duration_minutes=30,
        )


def test_find_free_slots_validates_duration() -> None:
    with pytest.raises(ValueError, match="duration_minutes"):
        cm.find_free_slots(
            ["personal"], time_min="2026-05-04T09:00:00+00:00",
            time_max="2026-05-04T17:00:00+00:00", duration_minutes=0,
        )


def test_find_free_slots_returns_full_window_when_no_busy(monkeypatch) -> None:
    svc = _make_service(freebusy_return={"calendars": {"primary": {"busy": []}}})
    monkeypatch.setattr(cm, "_service", lambda account: svc)

    free = cm.find_free_slots(
        ["personal"],
        time_min="2026-05-04T09:00:00+00:00",
        time_max="2026-05-04T17:00:00+00:00",
        duration_minutes=30,
    )
    assert len(free) == 1
    assert free[0]["start"].startswith("2026-05-04T09:00:00")
    assert free[0]["end"].startswith("2026-05-04T17:00:00")


def test_find_free_slots_inverts_busy_intervals(monkeypatch) -> None:
    svc = _make_service(freebusy_return={
        "calendars": {"primary": {"busy": [
            {"start": "2026-05-04T10:00:00+00:00", "end": "2026-05-04T11:00:00+00:00"},
            {"start": "2026-05-04T14:00:00+00:00", "end": "2026-05-04T15:00:00+00:00"},
        ]}}
    })
    monkeypatch.setattr(cm, "_service", lambda account: svc)

    free = cm.find_free_slots(
        ["personal"],
        time_min="2026-05-04T09:00:00+00:00",
        time_max="2026-05-04T17:00:00+00:00",
        duration_minutes=60,
    )
    # Expect 3 gaps: 09-10, 11-14, 15-17 — all >= 60 min.
    assert len(free) == 3
    assert free[0]["start"].startswith("2026-05-04T09:00:00")
    assert free[0]["end"].startswith("2026-05-04T10:00:00")
    assert free[1]["start"].startswith("2026-05-04T11:00:00")
    assert free[1]["end"].startswith("2026-05-04T14:00:00")
    assert free[2]["start"].startswith("2026-05-04T15:00:00")
    assert free[2]["end"].startswith("2026-05-04T17:00:00")


def test_find_free_slots_filters_short_gaps(monkeypatch) -> None:
    svc = _make_service(freebusy_return={
        "calendars": {"primary": {"busy": [
            {"start": "2026-05-04T09:30:00+00:00", "end": "2026-05-04T10:00:00+00:00"},
        ]}}
    })
    monkeypatch.setattr(cm, "_service", lambda account: svc)

    free = cm.find_free_slots(
        ["personal"],
        time_min="2026-05-04T09:00:00+00:00",
        time_max="2026-05-04T17:00:00+00:00",
        duration_minutes=60,
    )
    # 09:00–09:30 is only 30 min — filtered out. 10:00–17:00 remains.
    assert len(free) == 1
    assert free[0]["start"].startswith("2026-05-04T10:00:00")


def test_find_free_slots_merges_across_accounts(monkeypatch) -> None:
    services = {
        "personal": _make_service(freebusy_return={
            "calendars": {"primary": {"busy": [
                {"start": "2026-05-04T09:00:00+00:00", "end": "2026-05-04T10:00:00+00:00"},
            ]}}
        }),
        "work_admin": _make_service(freebusy_return={
            "calendars": {"primary": {"busy": [
                # overlaps with personal busy → should merge into a single 09:00–11:00 block
                {"start": "2026-05-04T09:30:00+00:00", "end": "2026-05-04T11:00:00+00:00"},
            ]}}
        }),
    }
    monkeypatch.setattr(cm, "_service", lambda account: services[account])

    free = cm.find_free_slots(
        ["personal", "work_admin"],
        time_min="2026-05-04T08:00:00+00:00",
        time_max="2026-05-04T17:00:00+00:00",
        duration_minutes=30,
    )
    # Free: 08:00–09:00 (60min) and 11:00–17:00 (6h). The 08:00–09:00 gap is 60min ≥ 30, kept.
    assert len(free) == 2
    assert free[0]["start"].startswith("2026-05-04T08:00:00")
    assert free[0]["end"].startswith("2026-05-04T09:00:00")
    assert free[1]["start"].startswith("2026-05-04T11:00:00")
    assert free[1]["end"].startswith("2026-05-04T17:00:00")


def test_list_configured_accounts_delegates_to_loader(monkeypatch) -> None:
    monkeypatch.setattr(cm, "list_accounts", lambda: ["personal", "work_admin"])
    assert cm.list_configured_accounts() == ["personal", "work_admin"]
