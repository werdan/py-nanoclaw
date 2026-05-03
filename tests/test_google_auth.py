from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nanoclaw.google_auth import (
    ALLOWED_ACCOUNTS,
    CredsError,
    list_accounts,
    load_credentials,
    upsert_account,
)


def test_list_accounts_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert list_accounts(path=tmp_path / "missing.json") == []


def test_load_credentials_unknown_account_raises(tmp_path: Path) -> None:
    with pytest.raises(CredsError, match="unknown account"):
        load_credentials("bogus", path=tmp_path / "creds.json")


def test_load_credentials_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(CredsError, match="not found"):
        load_credentials("personal", path=tmp_path / "creds.json")


def test_load_credentials_account_not_configured(tmp_path: Path) -> None:
    p = tmp_path / "creds.json"
    p.write_text(
        json.dumps({"client": {"client_id": "c", "client_secret": "s"}, "accounts": {}}),
        encoding="utf-8",
    )
    with pytest.raises(CredsError, match="not configured"):
        load_credentials("personal", path=p)


def test_load_credentials_malformed_file_raises(tmp_path: Path) -> None:
    p = tmp_path / "creds.json"
    p.write_text("not-json", encoding="utf-8")
    with pytest.raises(CredsError, match="not valid JSON"):
        load_credentials("personal", path=p)


def test_upsert_creates_file_with_0600_mode_and_round_trips(tmp_path: Path) -> None:
    p = tmp_path / "creds.json"
    upsert_account(
        "personal",
        refresh_token="rt-1",
        email="me@example.com",
        client_id="cid",
        client_secret="csec",
        scopes=["https://www.googleapis.com/auth/calendar.events"],
        path=p,
    )
    upsert_account(
        "work_admin",
        refresh_token="rt-2",
        email="admin@me.org",
        client_id="cid",
        client_secret="csec",
        scopes=["https://www.googleapis.com/auth/calendar.freebusy"],
        path=p,
    )

    assert list_accounts(path=p) == ["personal", "work_admin"]

    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["client"]["client_id"] == "cid"
    assert raw["client"]["client_secret"] == "csec"
    assert raw["accounts"]["personal"]["refresh_token"] == "rt-1"
    assert raw["accounts"]["personal"]["email"] == "me@example.com"
    # union of scopes from both upserts
    assert set(raw["scopes"]) == {
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.freebusy",
    }
    assert (p.stat().st_mode & 0o777) == 0o600


def test_upsert_replaces_refresh_token_for_same_account(tmp_path: Path) -> None:
    p = tmp_path / "creds.json"
    upsert_account(
        "personal", refresh_token="old", email="me@example.com",
        client_id="cid", client_secret="csec", scopes=[], path=p,
    )
    upsert_account(
        "personal", refresh_token="new", email="me@example.com",
        client_id="cid", client_secret="csec", scopes=[], path=p,
    )
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["accounts"]["personal"]["refresh_token"] == "new"


def test_load_credentials_returns_credentials_with_expected_fields(tmp_path: Path) -> None:
    p = tmp_path / "creds.json"
    upsert_account(
        "personal", refresh_token="rt-1", email="me@example.com",
        client_id="cid", client_secret="csec",
        scopes=["https://www.googleapis.com/auth/calendar.events"],
        path=p,
    )
    creds = load_credentials("personal", path=p)
    assert creds.refresh_token == "rt-1"
    assert creds.client_id == "cid"
    assert creds.client_secret == "csec"
    assert "https://www.googleapis.com/auth/calendar.events" in (creds.scopes or [])


def test_path_env_var_used_when_no_explicit_path(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "envcreds.json"
    monkeypatch.setenv("NANOCLAW_GOOGLE_CREDS_PATH", str(p))
    upsert_account(
        "personal", refresh_token="rt", email=None,
        client_id="cid", client_secret="csec", scopes=["s1"],
    )
    assert p.exists()
    assert list_accounts() == ["personal"]


def test_upsert_rejects_unknown_account(tmp_path: Path) -> None:
    with pytest.raises(CredsError, match="unknown account"):
        upsert_account(
            "boss", refresh_token="rt", email=None,
            client_id="cid", client_secret="csec", scopes=[], path=tmp_path / "x.json",
        )


def test_allowed_accounts_set() -> None:
    assert ALLOWED_ACCOUNTS == ("personal", "work_admin", "work_corp")
