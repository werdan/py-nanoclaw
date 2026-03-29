"""Docker entrypoint: stdin JSON → ``nanoclaw.claude_agent_run`` (same as local dev)."""

from __future__ import annotations

from nanoclaw.claude_agent_run import main

if __name__ == "__main__":
    raise SystemExit(main())
