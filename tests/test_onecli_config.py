from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from nanoclaw import onecli_config


def _set_onecli_env(monkeypatch, *, key: str = "key-1", url: str = "http://onecli:10255") -> None:
    monkeypatch.setenv("ONECLI_API_KEY", key)
    monkeypatch.setenv("ONECLI_URL", url)
    monkeypatch.delenv(onecli_config.PROXY_DISABLE_ENV, raising=False)
    monkeypatch.delenv(onecli_config.API_URL_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(onecli_config.AGENT_IDENTIFIER_ENV, raising=False)


def _stub_urlopen(payload: dict[str, Any]):
    body = json.dumps(payload).encode("utf-8")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return body

    def _open(*_args, **_kwargs):
        return _Resp()

    return _open


def test_is_enabled_false_when_creds_missing(monkeypatch) -> None:
    monkeypatch.delenv("ONECLI_API_KEY", raising=False)
    monkeypatch.delenv("ONECLI_URL", raising=False)
    assert onecli_config.is_enabled() is False


def test_is_enabled_false_when_kill_switch_set(monkeypatch) -> None:
    _set_onecli_env(monkeypatch)
    monkeypatch.setenv(onecli_config.PROXY_DISABLE_ENV, "1")
    assert onecli_config.is_enabled() is False


def test_is_enabled_true(monkeypatch) -> None:
    _set_onecli_env(monkeypatch)
    assert onecli_config.is_enabled() is True


def test_api_base_url_swaps_gateway_port_to_dashboard(monkeypatch) -> None:
    _set_onecli_env(monkeypatch, url="http://onecli:10255")
    assert onecli_config.api_base_url() == "http://onecli:10254"


def test_api_base_url_explicit_override_wins(monkeypatch) -> None:
    _set_onecli_env(monkeypatch)
    monkeypatch.setenv(onecli_config.API_URL_OVERRIDE_ENV, "https://dash.example/")
    assert onecli_config.api_base_url() == "https://dash.example"


def test_fetch_env_returns_env_dict(monkeypatch) -> None:
    _set_onecli_env(monkeypatch)
    payload = {"env": {"TELEGRAM_BOT_TOKEN": "tg-1", "OPENAI_API_KEY": "oa-1", "IGNORE_ME": 7}}
    with patch("nanoclaw.onecli_config.urlopen", _stub_urlopen(payload)):
        env = onecli_config.fetch_env()
    assert env == {"TELEGRAM_BOT_TOKEN": "tg-1", "OPENAI_API_KEY": "oa-1"}


def test_fetch_env_returns_empty_when_disabled(monkeypatch) -> None:
    monkeypatch.delenv("ONECLI_API_KEY", raising=False)
    monkeypatch.delenv("ONECLI_URL", raising=False)
    assert onecli_config.fetch_env() == {}


def test_fetch_env_returns_empty_on_network_error(monkeypatch) -> None:
    _set_onecli_env(monkeypatch)
    from urllib.error import URLError

    def _boom(*_args, **_kwargs):
        raise URLError("nope")

    with patch("nanoclaw.onecli_config.urlopen", _boom):
        assert onecli_config.fetch_env() == {}


def test_fetch_env_persists_ca_cert(monkeypatch, tmp_path: Path) -> None:
    _set_onecli_env(monkeypatch)
    ca_path = tmp_path / "ca.pem"
    payload = {
        "env": {"X": "1"},
        "caCertificate": "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n",
        "caCertificateContainerPath": str(ca_path),
    }
    with patch("nanoclaw.onecli_config.urlopen", _stub_urlopen(payload)):
        onecli_config.fetch_env()
    assert ca_path.exists()
    assert "BEGIN CERTIFICATE" in ca_path.read_text(encoding="utf-8")


def test_apply_to_environ_does_not_override_by_default(monkeypatch) -> None:
    monkeypatch.setenv("TOKEN_A", "from-env")
    monkeypatch.delenv("TOKEN_B", raising=False)
    applied = onecli_config.apply_to_environ({"TOKEN_A": "from-onecli", "TOKEN_B": "from-onecli"})
    import os
    assert os.environ["TOKEN_A"] == "from-env"
    assert os.environ["TOKEN_B"] == "from-onecli"
    assert applied == ["TOKEN_B"]


def test_apply_to_environ_override_flag(monkeypatch) -> None:
    monkeypatch.setenv("TOKEN_A", "from-env")
    applied = onecli_config.apply_to_environ({"TOKEN_A": "from-onecli"}, override=True)
    import os
    assert os.environ["TOKEN_A"] == "from-onecli"
    assert applied == ["TOKEN_A"]
