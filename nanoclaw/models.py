from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Inbound:
    """One user message from a channel adapter. Bot state lives in the agent."""

    content: str
    temp_paths: tuple[Path, ...] = ()
    created_at: float = field(default_factory=time.time)
