from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Inbound:
    """One user message from a channel adapter. Bot state lives in the agent."""

    content: str
    temp_paths: tuple[Path, ...] = ()
