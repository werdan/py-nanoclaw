# py-nanoclaw

A Python port of [nanoclaw](https://github.com/qwibitai/nanoclaw) -- a lightweight AI assistant framework powered by the Claude Agent SDK.

Compared to the original TypeScript implementation, py-nanoclaw favours simplicity: an in-memory `asyncio.Queue` replaces the SQLite message store, and the entire host process is a few hundred lines of Python.

## Architecture

```
Telegram ─► asyncio.Queue ─► worker loop ─► dispatch (HTTP to persistent agent) ─► reply queue ─► Telegram
```

**Bot container** (long-running) receives messages and manages the queue. **Agent container** is a separate long-running service with an HTTP endpoint. The bot posts prompt/session payloads to the agent, which calls Claude Agent SDK and returns JSON. The two containers are isolated -- the agent never sees Telegram secrets.

### Key components

| File | Purpose |
|------|---------|
| `nanoclaw/telegram_app.py` | Telegram channel (text, voice, images) |
| `nanoclaw/loop.py` | Async worker loop -- drains queue, dispatches batches |
| `nanoclaw/dispatch.py` | Sends HTTP requests to persistent agent service |
| `nanoclaw/cli.py` | Local REPL for testing without Telegram |
| `container/agent_runner.py` | Runs inside the agent container -- starts HTTP server |

## Security

- **User-locked**: Telegram handler rejects any user ID that isn't yours.
- **Container isolation**: The agent runs in a separate Docker container with no access to bot secrets. Claude credentials are provided via [OneCLI](https://github.com/onecli/onecli) (injected into the agent container as `ONECLI_*` variables).
- **Temp file cleanup**: Uploaded images are deleted after the agent processes them.

## Quick start

### Local development (CLI)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env   # fill in TELEGRAM_*, OPENAI_API_KEY, ONECLI_*
python -m nanoclaw.cli
```

### Docker Compose (Telegram)

Compose runs **Postgres** (for OneCLI’s DB), **OneCLI**, the **agent**, and the **bot**. Postgres is not published to the host; only OneCLI’s dashboard/gateway ports are bound to loopback.

```bash
# Build images
docker compose build
docker build -f container/Dockerfile -t nanoclaw-agent .

docker compose up -d
```

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | yes | Telegram bot token from @BotFather |
| `TELEGRAM_USER_ID` | yes | Your numeric Telegram user ID |
| `OPENAI_API_KEY` | for voice | Whisper transcription |
| `ONECLI_URL` | yes | OneCLI gateway URL for agent egress (default in compose: `http://onecli:10255`) |
| `ONECLI_API_KEY` | yes (after UI setup) | OneCLI dashboard API key (`oc_…`), used to fetch `/api/container-config` (SDK-style wiring) |
| `NANOCLAW_ONECLI_API_URL` | no | OneCLI API base URL override (default derives from `ONECLI_URL`, swapping `:10255` -> `:10254`) |
| `ONECLI_DB_PASSWORD` | no | Postgres password for the bundled `postgres` service (default `onecli`; must be URL-safe or avoid special chars in `DATABASE_URL`) |
| `NANOCLAW_AGENT_IMAGE` | no | Agent service image (default: `nanoclaw-agent`) |
| `NANOCLAW_AGENT_URL` | no | Bot → agent HTTP endpoint (default: `http://agent:8000/message`) |
| `NANOCLAW_AGENT_LOCAL` | no | Set `1` to run the Claude SDK on the host (no docker agent); see [LOCAL.md](LOCAL.md) |
| `NANOCLAW_ONECLI_CA_PATH` | with local + OneCLI | Host path to `gateway/ca.pem` for trusting the OneCLI MITM CA |
| `NANOCLAW_AGENT_TIMEOUT_S` | no | Agent timeout in seconds (default: 180) |

## Multimodal

- **Text**: passed directly to the agent.
- **Voice**: transcribed via OpenAI Whisper, then passed as text.
- **Images**: saved to a temp directory, mounted into the agent container. The agent uses Claude's `Read` tool to view the image. Temp files are cleaned up after dispatch.

## Scheduler behavior

- When a scheduling task is created, the bot sends an automatic acceptance message with task ID, cron expression, and next run time.
- The agent should not send a separate task-creation confirmation to avoid duplicate confirmations.

## Tests

```bash
pip install -e '.[dev]'
pytest
```

## License

MIT
