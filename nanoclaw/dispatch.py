"""Agent dispatch: run the agent container and persist session/result."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from nanoclaw.models import Inbound

logger = logging.getLogger(__name__)

_DEFAULT_AGENT_IMAGE = "nanoclaw-agent"
_DEFAULT_TIMEOUT_S = 180.0
# Compose attaches ephemeral agents only to this network (with OneCLI), not the default
# network where docker-socket-proxy lives.
_DEFAULT_DOCKER_NETWORK = "nanoclaw_agent"
_AGENT_CONTAINER_WORKDIR = "/work"
_DOCKER_HOST_SESSION_DIR_ENV = "NANOCLAW_DOCKER_HOST_SESSION_DIR"
_DOCKER_HOST_MEDIA_DIR_ENV = "NANOCLAW_DOCKER_HOST_MEDIA_DIR"
_DOCKER_HOST_PROJECT_DIR_ENV = "NANOCLAW_DOCKER_HOST_PROJECT_DIR"
_DOCKER_NETWORK_ENV = "NANOCLAW_DOCKER_NETWORK"
_MEDIA_DIR_ENV = "NANOCLAW_MEDIA_DIR"
# OneCLI gateway expects Proxy-Authorization: Basic base64("x:{agent_token}"); HTTP clients
# typically send that via HTTPS_PROXY=http://x:{token}@host:port (see onecli inject.rs).
_ONECLI_PROXY_DISABLE_ENV = "NANOCLAW_ONECLI_PROXY_DISABLE"
_ONECLI_API_URL_ENV = "NANOCLAW_ONECLI_API_URL"
_ONECLI_AGENT_IDENTIFIER_ENV = "NANOCLAW_ONECLI_AGENT"
_ONECLI_DATA_VOLUME_ENV = "NANOCLAW_ONECLI_DATA_VOLUME"
_ANTHROPIC_PLACEHOLDER_ENV = "NANOCLAW_ANTHROPIC_PLACEHOLDER_KEY"
_DEFAULT_ONECLI_DATA_VOLUME = "nanoclaw_onecli-data"
_ONECLI_CA_IN_CONTAINER = "/onecli-data/gateway/ca.pem"
# Run Claude SDK on the host (no docker agent); use with .env + NANOCLAW_ONECLI_CA_PATH for OneCLI.
_LOCAL_AGENT_ENV = "NANOCLAW_AGENT_LOCAL"
_HOST_CA_ENV = "NANOCLAW_ONECLI_CA_PATH"
_TASKS_PATH_ENV = "NANOCLAW_TASKS_PATH"


def _agent_image() -> str:
    return os.environ.get("NANOCLAW_AGENT_IMAGE", _DEFAULT_AGENT_IMAGE)


def _agent_timeout_s() -> float:
    raw = os.environ.get("NANOCLAW_AGENT_TIMEOUT_S")
    if raw is None or raw.strip() == "":
        return _DEFAULT_TIMEOUT_S
    return float(raw)


def _docker_network() -> str:
    raw = os.environ.get(_DOCKER_NETWORK_ENV)
    if raw is None or raw.strip() == "":
        return _DEFAULT_DOCKER_NETWORK
    return raw.strip()


def _use_onecli_http_proxy() -> bool:
    if os.environ.get(_ONECLI_PROXY_DISABLE_ENV, "").strip().lower() in ("1", "true", "yes"):
        return False
    return bool(
        os.environ.get("ONECLI_API_KEY", "").strip() and os.environ.get("ONECLI_URL", "").strip()
    )


def _onecli_agent_env_dict() -> dict[str, str]:
    """Legacy fallback env for Claude + HTTP clients to use OneCLI MITM proxy."""
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


def _onecli_api_base_url() -> str:
    """Resolve OneCLI API base URL (defaults to ONECLI_URL host with port 10254)."""
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


def _fetch_onecli_container_config() -> dict[str, Any]:
    """Fetch OneCLI SDK-style container config using Bearer ONECLI_API_KEY."""
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

    # Keep Nanoclaw's placeholder override if configured.
    placeholder = os.environ.get(_ANTHROPIC_PLACEHOLDER_ENV, "").strip()
    if placeholder:
        env["ANTHROPIC_API_KEY"] = placeholder

    ca_pem = raw.get("caCertificate")
    ca_path = raw.get("caCertificateContainerPath")
    if not isinstance(ca_pem, str):
        ca_pem = ""
    if not isinstance(ca_path, str) or not ca_path.strip():
        ca_path = ""
    return {"env": env, "ca_pem": ca_pem, "ca_path": ca_path}


def _onecli_runtime_for_docker(*, session_path: Path) -> tuple[dict[str, str], list[str], list[Path]]:
    """
    Return (env, volume_args, cleanup_paths) for OneCLI in docker-run agents.

    Prefers SDK-style `/api/container-config`; falls back to legacy proxy+volume wiring.
    """
    if not _use_onecli_http_proxy():
        return {}, [], []
    try:
        cfg = _fetch_onecli_container_config()
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("OneCLI container-config fetch failed, using legacy proxy wiring: %s", exc)
        cfg = {}
    except Exception as exc:
        logger.warning("OneCLI container-config unexpected error, using legacy proxy wiring: %s", exc)
        cfg = {}

    env = cfg.get("env")
    ca_pem = cfg.get("ca_pem")
    ca_path = cfg.get("ca_path")
    if isinstance(env, dict) and env:
        mounts: list[str] = []
        cleanup: list[Path] = []
        if isinstance(ca_pem, str) and ca_pem.strip():
            # IMPORTANT: docker bind source is resolved by the host daemon, not this process.
            # When bot runs in a container, write the temp cert into the session bind mount so
            # we can reference the corresponding host path via NANOCLAW_DOCKER_HOST_SESSION_DIR.
            session_container_dir = session_path.parent.resolve()
            host_session_dir = Path(
                os.environ.get(_DOCKER_HOST_SESSION_DIR_ENV, str(session_container_dir))
            ).resolve()
            name = f".nanoclaw-onecli-ca-{uuid.uuid4().hex}.pem"
            container_ca_file = (session_container_dir / name).resolve()
            container_ca_file.write_text(ca_pem, encoding="utf-8")
            host_ca = (
                (host_session_dir / name).resolve()
                if host_session_dir != session_container_dir
                else container_ca_file
            )
            container_ca = (
                ca_path.strip() if isinstance(ca_path, str) and ca_path.strip() else _ONECLI_CA_IN_CONTAINER
            )
            mounts.extend(["-v", f"{host_ca}:{container_ca}:ro"])
            cleanup.append(container_ca_file)
        return env, mounts, cleanup

    return _onecli_agent_env_dict(), _onecli_data_volume_args(), []


def _onecli_data_volume_args() -> list[str]:
    if not _use_onecli_http_proxy():
        return []
    vol = os.environ.get(_ONECLI_DATA_VOLUME_ENV, "").strip()
    if not vol:
        vol = _DEFAULT_ONECLI_DATA_VOLUME
    return ["-v", f"{vol}:/onecli-data:ro"]


def load_session_id(path: str | Path) -> str | None:
    """Read session_id from path; return None if missing/empty."""
    p = Path(path)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8").strip()
    return text or None


def save_session_id(path: str | Path, session_id: str) -> None:
    """Persist session_id for future resume calls."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(session_id.strip() + "\n", encoding="utf-8")
    os.chmod(p, 0o600)


def _agent_local_enabled() -> bool:
    return os.environ.get(_LOCAL_AGENT_ENV, "").strip().lower() in ("1", "true", "yes")


def _onecli_host_env_for_local() -> dict[str, str]:
    """Extra env for Claude subprocess on the host (CA path must exist on the host)."""
    if not _use_onecli_http_proxy():
        return {}
    base = _onecli_agent_env_dict()
    ca = os.environ.get(_HOST_CA_ENV, "").strip()
    if ca:
        return {
            **base,
            "SSL_CERT_FILE": ca,
            "REQUESTS_CA_BUNDLE": ca,
            "NODE_EXTRA_CA_CERTS": ca,
        }
    logger.warning(
        "NANOCLAW_AGENT_LOCAL with OneCLI proxy: set %s to a host path to gateway/ca.pem "
        "(copy from volume: docker run --rm -v nanoclaw_onecli-data:/d:ro alpine cat /d/gateway/ca.pem > /tmp/onecli-ca.pem)",
        _HOST_CA_ENV,
    )
    return base


async def _run_agent_local(payload: dict[str, Any]) -> dict[str, Any]:
    from nanoclaw.claude_agent_run import run_agent_payload

    extra = _onecli_host_env_for_local()
    return await run_agent_payload(payload, extra_env=extra)


def _docker_env_args(*, onecli_env: dict[str, str] | None = None) -> list[str]:
    args: list[str] = []
    for name in ("CLAUDE_MODEL", _TASKS_PATH_ENV):
        if name in os.environ:
            args.extend(["-e", name])
    if _use_onecli_http_proxy():
        env_map = onecli_env if onecli_env is not None else _onecli_agent_env_dict()
        for name, value in env_map.items():
            args.extend(["-e", f"{name}={value}"])
    else:
        keys = [k for k in os.environ if k.startswith("ONECLI_")]
        seen: set[str] = set()
        for key in keys:
            if key in seen:
                continue
            seen.add(key)
            args.extend(["-e", key])
    return args


def _docker_volume_args(
    *,
    session_path: Path,
    temp_paths: tuple[Path, ...],
) -> list[str]:
    session_container_dir = session_path.parent.resolve()
    host_session_dir = Path(
        os.environ.get(_DOCKER_HOST_SESSION_DIR_ENV, str(session_container_dir))
    ).resolve()

    media_container_dir_raw = os.environ.get(_MEDIA_DIR_ENV)
    media_host_dir_raw = os.environ.get(_DOCKER_HOST_MEDIA_DIR_ENV)
    media_container_dir = (
        Path(media_container_dir_raw).resolve()
        if media_container_dir_raw and media_container_dir_raw.strip()
        else None
    )
    media_host_dir = (
        Path(media_host_dir_raw).resolve()
        if media_host_dir_raw and media_host_dir_raw.strip()
        else None
    )

    # (host, container) -> "rw" | "ro"; session must stay rw for SDK transcripts.
    mount_mode: dict[tuple[Path, Path], str] = {}
    key_sess = (host_session_dir, session_container_dir)
    mount_mode[key_sess] = "rw"

    for path in temp_paths:
        container_parent = path.parent.resolve()
        host_parent = container_parent
        if media_container_dir and media_host_dir and container_parent.is_relative_to(media_container_dir):
            rel = container_parent.relative_to(media_container_dir)
            host_parent = (media_host_dir / rel).resolve()
        k = (host_parent, container_parent)
        if k not in mount_mode:
            mount_mode[k] = "ro"

    args: list[str] = []
    for (host_path, container_path), mode in sorted(
        mount_mode.items(), key=lambda item: (str(item[0][1]), str(item[0][0]))
    ):
        args.extend(["-v", f"{host_path}:{container_path}:{mode}"])
    return args


def _project_instruction_volume_args(*, session_path: Path) -> list[str]:
    session_container_dir = session_path.parent.resolve()
    host_session_dir = Path(
        os.environ.get(_DOCKER_HOST_SESSION_DIR_ENV, str(session_container_dir))
    ).resolve()

    host_project_raw = os.environ.get(_DOCKER_HOST_PROJECT_DIR_ENV, "").strip()
    host_project_dir = Path(host_project_raw).resolve() if host_project_raw else None
    if host_project_dir is None:
        # Derive project root from ".../runtime/sessions" when available.
        if host_session_dir.name == "sessions" and host_session_dir.parent.name == "runtime":
            host_project_dir = host_session_dir.parent.parent
        else:
            return []

    claude_md = host_project_dir / "CLAUDE.md"
    claude_dir = host_project_dir / ".claude"
    return [
        "-v",
        f"{claude_md}:/work/CLAUDE.md:ro",
        "-v",
        f"{claude_dir}:/work/.claude:ro",
    ]


def _log_agent_process_diagnostics(
    *,
    reason: str,
    command: list[str],
    returncode: int | None,
    stderr: str,
    stdout: str,
) -> None:
    """Log sanitized subprocess diagnostics without dumping unbounded output."""
    def _sanitize(text: str) -> str:
        out = text or "<empty>"
        # Redact obvious secret-bearing env assignments/tokens in command/log payloads.
        out = re.sub(
            r"\b(TELEGRAM_BOT_TOKEN|OPENAI_API_KEY|ONECLI_API_KEY|ANTHROPIC_API_KEY|HTTP_PROXY|HTTPS_PROXY|http_proxy|https_proxy)=\S+",
            r"\1=<redacted>",
            out,
        )
        out = re.sub(r"(Authorization|Proxy-Authorization):\s*\S+", r"\1: <redacted>", out)
        if len(out) > 4000:
            out = out[:4000] + "\n...<truncated>"
        return out

    command_s = _sanitize(" ".join(command))
    logger.error(
        "nanoclaw.agent: %s\n"
        "  docker argv: %s\n"
        "  exit_code: %s\n"
        "  stderr:\n%s\n"
        "  stdout:\n%s",
        reason,
        command_s,
        returncode,
        _sanitize(stderr),
        _sanitize(stdout),
    )


async def _run_agent_container(
    *,
    payload: dict[str, Any],
    session_path: Path,
    temp_paths: tuple[Path, ...],
) -> dict[str, Any]:
    onecli_env, onecli_mounts, cleanup_paths = _onecli_runtime_for_docker(session_path=session_path)
    try:
        command = [
            "docker",
            "run",
            "-i",
            "--rm",
            "--network",
            _docker_network(),
            "--memory",
            "512m",
            "--cpus",
            "1",
            *_docker_env_args(onecli_env=onecli_env),
            *_docker_volume_args(session_path=session_path, temp_paths=temp_paths),
            *_project_instruction_volume_args(session_path=session_path),
            *onecli_mounts,
            "-w",
            _AGENT_CONTAINER_WORKDIR,
            _agent_image(),
        ]
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        in_bytes = json.dumps(payload).encode("utf-8")
        timeout_s = _agent_timeout_s()
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(input=in_bytes),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError as exc:
            # Prevent orphaned docker-run clients (and attached containers) on timeout.
            proc.kill()
            out_b, err_b = await proc.communicate()
            out_s = out_b.decode("utf-8", errors="replace")
            err_s = err_b.decode("utf-8", errors="replace")
            rc = proc.returncode
            _log_agent_process_diagnostics(
                reason=f"timed out after {timeout_s:.1f}s (process killed)",
                command=command,
                returncode=rc,
                stderr=err_s,
                stdout=out_s,
            )
            raise RuntimeError(
                f"agent container timed out after {timeout_s:.1f}s and was killed; "
                f"stderr={err_s.strip() or '<empty>'}; stdout={out_s.strip() or '<empty>'}"
            ) from exc
        out_s = out_b.decode("utf-8", errors="replace")
        err_s = err_b.decode("utf-8", errors="replace")
        out_stripped = out_s.strip()
        err_stripped = err_s.strip()
        if proc.returncode != 0:
            _log_agent_process_diagnostics(
                reason="docker run exited non-zero",
                command=command,
                returncode=proc.returncode,
                stderr=err_s,
                stdout=out_s,
            )
            raise RuntimeError(
                f"agent container failed (exit {proc.returncode}); stderr={err_stripped or '<empty>'}; stdout={out_stripped or '<empty>'}"
            )
        try:
            data = json.loads(out_stripped)
        except json.JSONDecodeError as exc:
            _log_agent_process_diagnostics(
                reason="stdout is not valid JSON",
                command=command,
                returncode=proc.returncode,
                stderr=err_s,
                stdout=out_s,
            )
            raise RuntimeError(f"agent container returned invalid JSON: {out_stripped}") from exc
        if not isinstance(data, dict):
            _log_agent_process_diagnostics(
                reason="JSON root is not an object",
                command=command,
                returncode=proc.returncode,
                stderr=err_s,
                stdout=out_s,
            )
            raise RuntimeError("agent container output JSON must be an object")
        return data
    finally:
        for path in cleanup_paths:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                logger.debug("failed to delete temp OneCLI CA file: %s", path, exc_info=True)


async def dispatch(
    batch: list[Inbound],
    out_queue: asyncio.Queue[str],
    session_ref: list[str | None],
    session_path: Path,
) -> None:
    """
    Run one agent call for the combined batch text (Docker agent unless ``NANOCLAW_AGENT_LOCAL``).

    Pushes result text to ``out_queue`` only when it is a non-empty string; empty ``result`` is skipped.
    Persists ``session_id`` when present.
    """
    prompt = "\n".join(msg.content for msg in batch)
    temp_paths = tuple(path for msg in batch for path in msg.temp_paths)
    payload = {"prompt": prompt, "session_id": session_ref[0]}
    try:
        if _agent_local_enabled():
            result = await _run_agent_local(payload)
        else:
            result = await _run_agent_container(
                payload=payload,
                session_path=session_path,
                temp_paths=temp_paths,
            )
    except Exception:
        if session_ref[0]:
            logger.warning("Agent resume failed for persisted session; clearing and retrying once.")
            session_ref[0] = None
            session_path.unlink(missing_ok=True)
            payload = {"prompt": prompt, "session_id": None}
            if _agent_local_enabled():
                result = await _run_agent_local(payload)
            else:
                result = await _run_agent_container(
                    payload=payload,
                    session_path=session_path,
                    temp_paths=temp_paths,
                )
        else:
            raise
    status = result.get("status")
    if status != "success":
        logger.error(
            "nanoclaw.agent: JSON status is not success (exit 0). payload=%s",
            json.dumps(result, default=str),
        )
        raise RuntimeError(f"agent container status is not success: {result!r}")
    sid = result.get("session_id")
    if isinstance(sid, str) and sid:
        save_session_id(session_path, sid)
        session_ref[0] = sid
    text = result.get("result")
    if isinstance(text, str) and text:
        await out_queue.put(text)
