from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Inbound:
    """One user message from a channel adapter. Bot state lives in the agent."""

    content: str
    created_at: float = field(default_factory=time.time)
