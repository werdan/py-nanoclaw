"""Agent dispatch via persistent HTTP agent service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from nanoclaw.models import Inbound

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 180.0
_LOCAL_AGENT_ENV = "NANOCLAW_AGENT_LOCAL"
_HOST_CA_ENV = "NANOCLAW_ONECLI_CA_PATH"
_ONECLI_PROXY_DISABLE_ENV = "NANOCLAW_ONECLI_PROXY_DISABLE"
_ANTHROPIC_PLACEHOLDER_ENV = "NANOCLAW_ANTHROPIC_PLACEHOLDER_KEY"
_AGENT_URL_ENV = "NANOCLAW_AGENT_URL"


def _agent_timeout_s() -> float:
    raw = os.environ.get("NANOCLAW_AGENT_TIMEOUT_S")
    if raw is None or raw.strip() == "":
        return _DEFAULT_TIMEOUT_S
    return float(raw)


def load_session_id(path: str | Path) -> str | None:
    p = Path(path)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8").strip()
    return text or None


def save_session_id(path: str | Path, session_id: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(session_id.strip() + "\n", encoding="utf-8")
    os.chmod(p, 0o600)


def _agent_local_enabled() -> bool:
    return os.environ.get(_LOCAL_AGENT_ENV, "").strip().lower() in ("1", "true", "yes")


def _onecli_host_env_for_local() -> dict[str, str]:
    if os.environ.get(_ONECLI_PROXY_DISABLE_ENV, "").strip().lower() in ("1", "true", "yes"):
        return {}
    key = os.environ.get("ONECLI_API_KEY", "").strip()
    url_s = os.environ.get("ONECLI_URL", "").strip()
    if not key or not url_s:
        return {}
    from urllib.parse import quote, urlparse

    parsed = urlparse(url_s)
    netloc = parsed.netloc or "onecli:10255"
    encoded = quote(key, safe="")
    proxy = f"http://x:{encoded}@{netloc}"
    ca = os.environ.get(_HOST_CA_ENV, "").strip()
    placeholder = os.environ.get(_ANTHROPIC_PLACEHOLDER_ENV, "placeholder").strip() or "placeholder"
    out = {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "ANTHROPIC_API_KEY": placeholder,
    }
    if ca:
        out.update(
            {
                "SSL_CERT_FILE": ca,
                "REQUESTS_CA_BUNDLE": ca,
                "NODE_EXTRA_CA_CERTS": ca,
            }
        )
    return out


async def _run_agent_local(payload: dict[str, Any]) -> dict[str, Any]:
    from nanoclaw.claude_agent_run import run_agent_payload

    return await run_agent_payload(payload, extra_env=_onecli_host_env_for_local())


async def _run_agent_http(payload: dict[str, Any]) -> dict[str, Any]:
    agent_url = os.environ.get(_AGENT_URL_ENV, "").strip()
    if not agent_url:
        raise RuntimeError(f"{_AGENT_URL_ENV} is required for persistent agent dispatch")
    timeout_s = _agent_timeout_s()

    def _post_json() -> dict[str, Any]:
        req = Request(
            agent_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise RuntimeError("agent HTTP response JSON must be an object")
        return data

    try:
        return await asyncio.to_thread(_post_json)
    except Exception as exc:
        raise RuntimeError(f"agent HTTP request failed: {exc}") from exc


async def dispatch(
    batch: list[Inbound],
    out_queue: asyncio.Queue[str],
    session_ref: list[str | None],
    session_path: Path,
) -> None:
    prompt = "\n".join(msg.content for msg in batch)
    payload = {"prompt": prompt, "session_id": session_ref[0]}
    try:
        result = (
            await _run_agent_local(payload) if _agent_local_enabled() else await _run_agent_http(payload)
        )
    except Exception:
        if session_ref[0]:
            logger.warning("Agent resume failed for persisted session; clearing and retrying once.")
            session_ref[0] = None
            session_path.unlink(missing_ok=True)
            payload = {"prompt": prompt, "session_id": None}
            result = (
                await _run_agent_local(payload)
                if _agent_local_enabled()
                else await _run_agent_http(payload)
            )
        else:
            raise
    status = result.get("status")
    if status != "success":
        logger.error(
            "nanoclaw.agent: JSON status is not success (exit 0). payload=%s",
            json.dumps(result, default=str),
        )
        raise RuntimeError(f"agent status is not success: {result!r}")
    sid = result.get("session_id")
    if isinstance(sid, str) and sid:
        save_session_id(session_path, sid)
        session_ref[0] = sid
    text = result.get("result")
    if isinstance(text, str) and text:
        await out_queue.put(text)
