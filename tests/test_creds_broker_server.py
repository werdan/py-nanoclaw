from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# The broker server lives under container/creds_broker/ since it's its own
# Docker image; expose it on sys.path so we can unit-test the dispatch logic
# without launching a container.
_BROKER_DIR = Path(__file__).resolve().parent.parent / "container" / "creds_broker"
sys.path.insert(0, str(_BROKER_DIR))
import server as broker  # noqa: E402


def _write_google_store(tmp_path: Path) -> Path:
    p = tmp_path / "google-oauth-creds.json"
    p.write_text(
        json.dumps({
            "client": {"client_id": "cid", "client_secret": "csec",
                        "token_uri": "https://oauth2.googleapis.com/token"},
            "scopes": ["https://www.googleapis.com/auth/calendar.events"],
            "accounts": {
                "personal": {"refresh_token": "rt-1", "email": "me@example.com"},
            },
        }),
        encoding="utf-8",
    )
    return p


def test_load_google_store_round_trip(tmp_path: Path) -> None:
    p = _write_google_store(tmp_path)
    store = broker._load_google_store(p)
    assert store["accounts"]["personal"]["refresh_token"] == "rt-1"


def test_load_google_store_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        broker._load_google_store(tmp_path / "nope.json")


def test_refresh_google_access_token_unknown_account_returns_error(tmp_path: Path) -> None:
    store = broker._load_google_store(_write_google_store(tmp_path))
    assert broker._refresh_google_access_token(store, "bogus") == {
        "ok": False, "error": "unknown account 'bogus'",
    }


def test_refresh_google_access_token_unconfigured_account(tmp_path: Path) -> None:
    store = broker._load_google_store(_write_google_store(tmp_path))
    resp = broker._refresh_google_access_token(store, "work_admin")
    assert resp == {"ok": False, "error": "account 'work_admin' not configured"}


def test_refresh_google_access_token_success(monkeypatch, tmp_path: Path) -> None:
    """With Credentials.refresh mocked, the broker should return a Bearer header
    + ISO-formatted expiry without hitting the network."""
    from datetime import datetime, timezone

    store = broker._load_google_store(_write_google_store(tmp_path))

    fixed_expiry = datetime(2026, 5, 4, 7, 0, 0, tzinfo=timezone.utc)

    def fake_refresh(self, request) -> None:
        self.token = "ya29.fake-access"
        # google-auth normally sets a naive UTC expiry; emulate that.
        self.expiry = fixed_expiry.replace(tzinfo=None)

    monkeypatch.setattr(broker.Credentials, "refresh", fake_refresh)

    resp = broker._refresh_google_access_token(store, "personal")
    assert resp["ok"] is True
    assert resp["authorization"] == "Bearer ya29.fake-access"
    assert resp["expires_at"].startswith("2026-05-04T07:00:00")


def test_read_telegram_token_present(tmp_path: Path) -> None:
    (tmp_path / "telegram-bot-token").write_text("8360:fake\n", encoding="utf-8")
    assert broker._read_telegram_token(tmp_path) == {"ok": True, "token": "8360:fake"}


def test_read_telegram_token_missing(tmp_path: Path) -> None:
    resp = broker._read_telegram_token(tmp_path)
    assert resp["ok"] is False
    assert "not at" in resp["error"]


def test_read_telegram_token_empty(tmp_path: Path) -> None:
    (tmp_path / "telegram-bot-token").write_text("", encoding="utf-8")
    resp = broker._read_telegram_token(tmp_path)
    assert resp == {"ok": False, "error": "telegram-bot-token file is empty"}


def test_agent_dispatch_rejects_telegram_op(tmp_path: Path) -> None:
    """Mount-based isolation: agent socket must not vend bot secrets even if asked."""
    resp = broker._agent_dispatch(tmp_path, {"op": "telegram_bot_token"})
    assert resp["ok"] is False
    assert "agent socket" in resp["error"]


def test_bot_dispatch_rejects_google_op(tmp_path: Path) -> None:
    resp = broker._bot_dispatch(tmp_path, {"op": "google_access_token", "account": "personal"})
    assert resp["ok"] is False
    assert "bot socket" in resp["error"]


def test_agent_dispatch_missing_account(tmp_path: Path) -> None:
    resp = broker._agent_dispatch(tmp_path, {"op": "google_access_token"})
    assert resp == {"ok": False, "error": "missing or non-string 'account'"}
