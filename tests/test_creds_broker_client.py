from __future__ import annotations

import json
import os
import socket
import threading
import tempfile
from pathlib import Path
from typing import Any

import pytest

from nanoclaw import creds_broker_client


@pytest.fixture
def short_tmpdir():
    """tmp_path can exceed the 104-char AF_UNIX path limit on macOS — use a
    short ``/tmp/...`` directory instead for socket creation."""
    with tempfile.TemporaryDirectory(prefix="brk-", dir="/tmp") as d:
        yield Path(d)


def _serve_one(sock_path: str, response: dict[str, Any]) -> threading.Thread:
    """Spin up a tiny one-shot UDS server that returns ``response`` to one
    request and exits. Used to unit-test the client without booting the full
    broker container."""

    def _run() -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(sock_path)
            s.listen(1)
            s.settimeout(5)
            conn, _ = s.accept()
            with conn:
                f = conn.makefile("rwb")
                _ = f.readline()  # consume request
                conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
        finally:
            s.close()
            try:
                os.unlink(sock_path)
            except FileNotFoundError:
                pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # Give the server a moment to bind.
    for _ in range(50):
        if Path(sock_path).exists():
            break
        import time
        time.sleep(0.01)
    return t


def test_fetch_google_access_token_returns_authorization(short_tmpdir: Path) -> None:
    sock = short_tmpdir / "agent.sock"
    _serve_one(
        str(sock),
        {"ok": True, "authorization": "Bearer ya29.test", "expires_at": "2026-05-04T07:00:00+00:00"},
    )
    resp = creds_broker_client.fetch_google_access_token("personal", sock_path=str(sock))
    assert resp["authorization"] == "Bearer ya29.test"
    assert resp["expires_at"].startswith("2026-05-04")


def test_fetch_telegram_bot_token_returns_string(short_tmpdir: Path) -> None:
    sock = short_tmpdir / "bot.sock"
    _serve_one(str(sock), {"ok": True, "token": "8360:fake"})
    token = creds_broker_client.fetch_telegram_bot_token(sock_path=str(sock))
    assert token == "8360:fake"


def test_broker_error_raised_on_ok_false(short_tmpdir: Path) -> None:
    sock = short_tmpdir / "bad.sock"
    _serve_one(str(sock), {"ok": False, "error": "no such account"})
    with pytest.raises(creds_broker_client.BrokerError, match="no such account"):
        creds_broker_client.fetch_google_access_token("personal", sock_path=str(sock))


def test_missing_socket_raises(short_tmpdir: Path) -> None:
    with pytest.raises(creds_broker_client.BrokerError, match="not found"):
        creds_broker_client.fetch_telegram_bot_token(sock_path=str(short_tmpdir / "missing.sock"))


def test_is_agent_broker_available(monkeypatch, short_tmpdir: Path) -> None:
    monkeypatch.delenv(creds_broker_client.AGENT_SOCKET_ENV, raising=False)
    assert creds_broker_client.is_agent_broker_available() is False

    sock = short_tmpdir / "live.sock"
    sock.touch()
    monkeypatch.setenv(creds_broker_client.AGENT_SOCKET_ENV, str(sock))
    assert creds_broker_client.is_agent_broker_available() is True


def test_fetch_without_env_raises(monkeypatch) -> None:
    monkeypatch.delenv(creds_broker_client.AGENT_SOCKET_ENV, raising=False)
    with pytest.raises(creds_broker_client.BrokerError, match="not set"):
        creds_broker_client.fetch_google_access_token("personal")
