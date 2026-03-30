# py-nanoclaw

A Python port of [nanoclaw](https://github.com/qwibitai/nanoclaw) -- a lightweight AI assistant framework powered by the Claude Agent SDK.

Compared to the original TypeScript implementation, py-nanoclaw favours simplicity: an in-memory `asyncio.Queue` replaces the SQLite message store, and the entire host process is a few hundred lines of Python.

## Architecture

```
Telegram ─► asyncio.Queue ─► worker loop ─► dispatch (spawns agent container) ─► reply queue ─► Telegram
```

**Bot container** (long-running) receives messages, manages the queue, and spawns ephemeral **agent containers** via Docker. The agent calls the Claude Agent SDK with session resumption and returns a JSON result on stdout. The two containers are isolated -- the agent never sees channel tokens or other secrets.

### Key components

| File | Purpose |
|------|---------|
| `nanoclaw/telegram_app.py` | Telegram channel (text, voice, images) |
| `nanoclaw/loop.py` | Async worker loop -- drains queue, dispatches batches |
| `nanoclaw/dispatch.py` | Spawns agent containers, reads JSON result |
| `nanoclaw/cli.py` | Local REPL for testing without Telegram |
| `container/agent_runner.py` | Runs inside the agent container -- stdin JSON in, stdout JSON out |

## Security

- **User-locked**: Telegram handler rejects any user ID that isn't yours.
- **Container isolation**: The agent runs in a separate Docker container with no access to bot secrets. Claude credentials are provided via [OneCLI](https://github.com/onecli/onecli) (injected into the agent container as `ONECLI_*` variables).
- **Docker socket proxy**: The bot talks to Docker through [Tecnativa/docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy), restricted to container operations only. Ephemeral **agent** containers attach only to **`nanoclaw_agent`** (with OneCLI), not the default network where the socket proxy runs.
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

Compose runs **Postgres** (for OneCLI’s DB), **OneCLI**, the **bot**, and **docker-socket-proxy**. Postgres is not published to the host; only OneCLI’s dashboard/gateway ports are bound to loopback.

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
| `NANOCLAW_AGENT_IMAGE` | no | Agent container image (default: `nanoclaw-agent`) |
| `NANOCLAW_AGENT_LOCAL` | no | Set `1` to run the Claude SDK on the host (no docker agent); see [LOCAL.md](LOCAL.md) |
| `NANOCLAW_ONECLI_CA_PATH` | with local + OneCLI | Host path to `gateway/ca.pem` for trusting the OneCLI MITM CA |
| `NANOCLAW_AGENT_TIMEOUT_S` | no | Agent timeout in seconds (default: 180) |
| `NANOCLAW_DOCKER_NETWORK` | no | Network for agent containers (default `nanoclaw_agent`; must reach OneCLI, not the socket proxy) |

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
