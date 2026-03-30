"""Persistent agent HTTP server.

POST /message with JSON payload:
  {"prompt": "...", "session_id": "...|null"}
Returns the same JSON shape as ``nanoclaw.claude_agent_run.main``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from nanoclaw.claude_agent_run import run_agent_payload

logger = logging.getLogger(__name__)

_ONECLI_API_URL_ENV = "NANOCLAW_ONECLI_API_URL"
_ONECLI_AGENT_IDENTIFIER_ENV = "NANOCLAW_ONECLI_AGENT"
_ANTHROPIC_PLACEHOLDER_ENV = "NANOCLAW_ANTHROPIC_PLACEHOLDER_KEY"
_ONECLI_PROXY_DISABLE_ENV = "NANOCLAW_ONECLI_PROXY_DISABLE"
_ONECLI_CA_IN_CONTAINER = "/onecli-data/gateway/ca.pem"


def _use_onecli_http_proxy() -> bool:
    if os.environ.get(_ONECLI_PROXY_DISABLE_ENV, "").strip().lower() in ("1", "true", "yes"):
        return False
    return bool(
        os.environ.get("ONECLI_API_KEY", "").strip() and os.environ.get("ONECLI_URL", "").strip()
    )


def _onecli_api_base_url() -> str:
    override = os.environ.get(_ONECLI_API_URL_ENV, "").strip()
    if override:
        return override.rstrip("/")
    gateway = os.environ.get("ONECLI_URL", "").strip()
    if not gateway:
        return ""
    parsed = urlparse(gateway)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "onecli"
    port = parsed.port
    if port == 10255:
        port = 10254
    netloc = f"{host}:{port}" if port else host
    return f"{scheme}://{netloc}"


def _onecli_legacy_env() -> dict[str, str]:
    key = os.environ.get("ONECLI_API_KEY", "").strip()
    url_s = os.environ.get("ONECLI_URL", "").strip()
    if not key or not url_s:
        return {}
    parsed = urlparse(url_s)
    netloc = parsed.netloc or "onecli:10255"
    encoded = quote(key, safe="")
    proxy = f"http://x:{encoded}@{netloc}"
    placeholder = os.environ.get(_ANTHROPIC_PLACEHOLDER_ENV, "placeholder").strip() or "placeholder"
    return {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "SSL_CERT_FILE": _ONECLI_CA_IN_CONTAINER,
        "REQUESTS_CA_BUNDLE": _ONECLI_CA_IN_CONTAINER,
        "NODE_EXTRA_CA_CERTS": _ONECLI_CA_IN_CONTAINER,
        "ANTHROPIC_API_KEY": placeholder,
    }


def _fetch_onecli_container_config() -> dict[str, Any]:
    key = os.environ.get("ONECLI_API_KEY", "").strip()
    base = _onecli_api_base_url()
    if not key or not base:
        return {}
    agent_identifier = os.environ.get(_ONECLI_AGENT_IDENTIFIER_ENV, "").strip()
    query = f"?{urlencode({'agent': agent_identifier})}" if agent_identifier else ""
    req = Request(
        f"{base}/api/container-config{query}",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urlopen(req, timeout=5) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    if not isinstance(raw, dict):
        return {}
    env_raw = raw.get("env")
    env: dict[str, str] = {}
    if isinstance(env_raw, dict):
        for k, v in env_raw.items():
            if isinstance(k, str) and isinstance(v, str):
                env[k] = v
    placeholder = os.environ.get(_ANTHROPIC_PLACEHOLDER_ENV, "").strip()
    if placeholder:
        env["ANTHROPIC_API_KEY"] = placeholder

    ca_pem = raw.get("caCertificate")
    ca_path = raw.get("caCertificateContainerPath")
    if isinstance(ca_pem, str) and ca_pem.strip():
        path = str(ca_path).strip() if isinstance(ca_path, str) and str(ca_path).strip() else "/tmp/onecli-gateway-ca.pem"
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(ca_pem, encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
    return env


def _resolve_extra_env() -> dict[str, str]:
    if not _use_onecli_http_proxy():
        return {}
    try:
        env = _fetch_onecli_container_config()
        if env:
            return env
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("OneCLI container-config fetch failed, using legacy proxy env: %s", exc)
    except Exception as exc:
        logger.warning("OneCLI config fetch unexpected error, using legacy proxy env: %s", exc)
    return _onecli_legacy_env()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, {"ok": True})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/message":
            self._json(404, {"status": "error", "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Input must be a JSON object.")
            out = asyncio.run(run_agent_payload(payload, extra_env=_resolve_extra_env()))
            self._json(200, out)
        except Exception as exc:
            logger.exception("agent request failed")
            self._json(500, {"status": "error", "error": str(exc)})

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        logger.info("agent_http: " + format, *args)

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        body = (json.dumps(payload) + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    host = os.environ.get("NANOCLAW_AGENT_BIND_HOST", "0.0.0.0")
    port = int(os.environ.get("NANOCLAW_AGENT_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), _Handler)
    logger.info("agent server listening on %s:%s", host, port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    t.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
