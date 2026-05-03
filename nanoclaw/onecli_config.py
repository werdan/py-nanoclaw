"""Bootstrap helper: fetch the OneCLI container-config env dict and apply it
to ``os.environ``. Lets the bot pull `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`,
etc. from the OneCLI secret vault instead of plaintext `.env` on disk.

OneCLI is the project's secret broker (already used by the agent for Anthropic
credential injection). Secrets are stored in OneCLI's dashboard and exposed
through ``/api/container-config`` along with a CA cert for the proxy chain.

Bootstrap order in callers:
    1. ``load_dotenv()`` — needs `ONECLI_API_KEY` itself to authenticate the fetch.
    2. ``apply_to_environ(fetch_env())`` — fill in any keys OneCLI provides that
       aren't already set in the environment.
    3. Read tokens via ``os.environ.get(...)`` as before.

By default ``apply_to_environ`` is non-destructive: a pre-existing `.env` value
wins over OneCLI for the same key. This makes the .env→OneCLI migration safe in
either direction. Pass ``override=True`` to flip the precedence.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Mapping
from urllib.error import URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

API_URL_OVERRIDE_ENV = "NANOCLAW_ONECLI_API_URL"
AGENT_IDENTIFIER_ENV = "NANOCLAW_ONECLI_AGENT"
PROXY_DISABLE_ENV = "NANOCLAW_ONECLI_PROXY_DISABLE"


def is_enabled() -> bool:
    """OneCLI bootstrap is "active" if both API key and gateway URL are configured
    and the explicit kill switch isn't set."""
    if os.environ.get(PROXY_DISABLE_ENV, "").strip().lower() in ("1", "true", "yes"):
        return False
    return bool(
        os.environ.get("ONECLI_API_KEY", "").strip()
        and os.environ.get("ONECLI_URL", "").strip()
    )


def api_base_url() -> str:
    """Resolve the OneCLI dashboard API URL from `NANOCLAW_ONECLI_API_URL` or by
    rewriting `ONECLI_URL`'s gateway port (10255) to the dashboard port (10254)."""
    override = os.environ.get(API_URL_OVERRIDE_ENV, "").strip()
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


def fetch_env(*, timeout_s: float = 5.0) -> dict[str, str]:
    """Fetch the OneCLI container-config env dict. Returns {} on any failure
    (network error, malformed JSON, OneCLI not configured) — callers should
    treat absence as "use whatever's already in os.environ"."""
    if not is_enabled():
        return {}
    key = os.environ.get("ONECLI_API_KEY", "").strip()
    base = api_base_url()
    if not key or not base:
        return {}
    agent_id = os.environ.get(AGENT_IDENTIFIER_ENV, "").strip()
    query = f"?{urlencode({'agent': agent_id})}" if agent_id else ""
    req = Request(
        f"{base}/api/container-config{query}",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("OneCLI container-config fetch failed: %s", exc)
        return {}
    if not isinstance(raw, dict):
        return {}

    env_raw = raw.get("env")
    env: dict[str, str] = {}
    if isinstance(env_raw, dict):
        for k, v in env_raw.items():
            if isinstance(k, str) and isinstance(v, str):
                env[k] = v

    # Persist the gateway CA cert if OneCLI shipped one — required when callers
    # also use OneCLI as an HTTPS MITM proxy. Best-effort; non-fatal on failure.
    ca_pem = raw.get("caCertificate")
    ca_path = raw.get("caCertificateContainerPath")
    if isinstance(ca_pem, str) and ca_pem.strip():
        path = (
            str(ca_path).strip()
            if isinstance(ca_path, str) and str(ca_path).strip()
            else "/tmp/onecli-gateway-ca.pem"
        )
        p = Path(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(ca_pem, encoding="utf-8")
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
        except OSError as exc:
            logger.warning("could not persist OneCLI CA cert at %s: %s", path, exc)
    return env


def apply_to_environ(env: Mapping[str, str], *, override: bool = False) -> list[str]:
    """Merge ``env`` into ``os.environ``. Returns the list of keys actually applied
    (i.e. that didn't already have a non-empty value, unless ``override=True``)."""
    applied: list[str] = []
    for k, v in env.items():
        if not override and os.environ.get(k):
            continue
        os.environ[k] = v
        applied.append(k)
    return applied
