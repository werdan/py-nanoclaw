from __future__ import annotations

from pathlib import Path

import pytest

from nanoclaw import openai_client


def _clear_env(monkeypatch) -> None:
    for k in ("ONECLI_AGENT_TOKEN", "ONECLI_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_returns_none_when_no_auth(monkeypatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    assert openai_client.build_async_openai_client(ca_path=str(tmp_path / "missing")) is None


def test_uses_direct_key_when_only_env_key_set(monkeypatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-direct")
    client = openai_client.build_async_openai_client(ca_path=str(tmp_path / "missing"))
    assert client is not None
    assert client.api_key == "sk-direct"


def _stub_ssl_context(monkeypatch) -> None:
    """Bypass real cert parsing — we don't need a valid CA in unit tests, just
    a parseable SSLContext object that httpx will accept."""
    import ssl

    monkeypatch.setattr(
        ssl, "create_default_context", lambda *a, **kw: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    )


def test_uses_proxy_when_onecli_and_ca_present(monkeypatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("ONECLI_AGENT_TOKEN", "aoc_test_token")
    monkeypatch.setenv("ONECLI_URL", "http://onecli:10255")
    ca = tmp_path / "ca.pem"
    ca.write_text("dummy", encoding="utf-8")
    _stub_ssl_context(monkeypatch)

    client = openai_client.build_async_openai_client(ca_path=str(ca))
    assert client is not None
    # API key sent over the wire is a placeholder — OneCLI swaps it.
    assert client.api_key == "placeholder"


def test_proxy_path_falls_back_to_direct_when_ca_missing(monkeypatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("ONECLI_AGENT_TOKEN", "aoc_test")
    monkeypatch.setenv("ONECLI_URL", "http://onecli:10255")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fallback")
    # ca_path doesn't exist → proxy can't verify → fall through to direct key.
    client = openai_client.build_async_openai_client(ca_path=str(tmp_path / "missing"))
    assert client is not None
    assert client.api_key == "sk-fallback"


def test_proxy_url_quotes_token(monkeypatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("ONECLI_AGENT_TOKEN", "abc/xyz=&special")
    monkeypatch.setenv("ONECLI_URL", "http://onecli:10255")
    ca = tmp_path / "ca.pem"
    ca.write_text("dummy", encoding="utf-8")
    _stub_ssl_context(monkeypatch)

    # Should not raise; httpx accepts the URL after percent-encoding.
    client = openai_client.build_async_openai_client(ca_path=str(ca))
    assert client is not None
