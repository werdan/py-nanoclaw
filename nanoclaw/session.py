from __future__ import annotations

from pathlib import Path


def load_session_id(path: str | Path) -> str | None:
    """
    Read ``session_id`` from ``path``. Returns ``None`` if the file is missing or empty.

    The SDK supplies the id (e.g. after the first ``query()`` / init message); persist it with
    :func:`save_session_id`.
    """
    p = Path(path)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8").strip()
    return text or None


def save_session_id(path: str | Path, session_id: str) -> None:
    """Write ``session_id`` so it survives process restarts."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(session_id.strip() + "\n", encoding="utf-8")
