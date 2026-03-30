"""Docker entrypoint: persistent HTTP agent server."""

from __future__ import annotations

from nanoclaw.agent_http_server import main

if __name__ == "__main__":
    raise SystemExit(main())
