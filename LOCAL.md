# Run locally with Docker

Use this before deploying to GCP. Requires [Docker Desktop](https://docs.docker.com/desktop/) (macOS/Windows) or Docker Engine on Linux.

## 1) Environment

```bash
cp .env.example .env
# Fill TELEGRAM_*, OPENAI_API_KEY, then OneCLI (see below)
```

### OneCLI (first time)

After the stack is running once, open **http://127.0.0.1:10254**, add an **Anthropic** secret for **api.anthropic.com**, and copy the OneCLI **API key** (`oc_...`) into `.env` as **`ONECLI_API_KEY`**. Use **`ONECLI_URL=http://onecli:10255`**. Restart **`bot`** (`docker compose up -d bot`). See **[DEPLOY.md](DEPLOY.md)** (section *OneCLI setup*) for remote SSH tunnel instructions.

## 2) Start stack

From the repo root:

```bash
bash ops/local_docker_up.sh
```

The script:

- sets `COMPOSE_PROJECT_NAME=nanoclaw` so the default bridge is **`nanoclaw_default`**; agent containers use **`nanoclaw_agent`** (with OneCLI only, not the Docker socket proxy)
- builds **`nanoclaw-agent`**
- runs **`docker compose up`** (bot, OneCLI, docker-socket-proxy)

## 3) Logs and stop

```bash
docker compose logs -f bot
# Ctrl+C, then:
docker compose down
```

## Local agent (fast iteration, no docker agent rebuild)

Use this to exercise the same Claude + OneCLI path as production **on your machine** while OneCLI is running in Compose. You avoid rebuilding `nanoclaw-agent` on every change.

1. Copy the gateway CA to the host (once per machine, or after volume reset):

   ```bash
   docker run --rm -v nanoclaw_onecli-data:/d:ro alpine cat /d/gateway/ca.pem > /tmp/onecli-ca.pem
   ```

2. In `.env` (or export in the shell), set:

   - `NANOCLAW_AGENT_LOCAL=1`
   - `ONECLI_URL=http://127.0.0.1:10255` (gateway on localhost while compose is up)
- `NANOCLAW_ONECLI_API_URL=http://127.0.0.1:10254` (needed when running Nanoclaw on host; default `onecli` DNS only resolves inside Docker)
   - `NANOCLAW_ONECLI_CA_PATH=/tmp/onecli-ca.pem`
   - Keep `ONECLI_API_KEY` and other OneCLI-related values as for Docker.

3. Install deps and run the interactive CLI (same queue/dispatch as the bot, but in-process agent):

   ```bash
   pip install -e ".[dev]"
   python -m nanoclaw.cli
   ```

**One-shot agent** (stdin JSON, stdout JSON — same protocol as the Docker agent):

```bash
echo '{"prompt":"Say hello in one word.","session_id":null}' | python -m nanoclaw.claude_agent_run
```

That command **does not** inject the OneCLI proxy or placeholder key unless you export the same variables `dispatch` would set (`HTTPS_PROXY`, `SSL_CERT_FILE`, `ANTHROPIC_API_KEY=placeholder`, etc.). For a test that matches production, prefer **`python -m nanoclaw.cli`** with `NANOCLAW_AGENT_LOCAL=1` and the variables in step 2, or export those vars before the one-liner.

The Telegram bot still expects Docker for dispatch unless you run it with `NANOCLAW_AGENT_LOCAL=1` in its environment (advanced; normally use the CLI for local tests).

## Notes

- **OneCLI API key scope**: `ONECLI_API_KEY` is used to fetch `/api/container-config`; the gateway then uses the returned proxy credentials internally. If config fetch fails from host-run tests, set `NANOCLAW_ONECLI_API_URL=http://127.0.0.1:10254`.
- **Socket proxy**: the bot talks to Docker via `docker-socket-proxy`, not the raw host socket.
- **Postgres**: OneCLI uses a local `postgres:16-alpine` service; data is in the `pgdata` volume (not exposed on host ports).
- **Ports**: OneCLI publishes `10254` / `10255` on localhost for dashboard and gateway.
- **Failures**: `dispatch` is fail-fast. If the agent call fails, Telegram gets a short error message (see `nanoclaw/telegram_app.py`); resend the message to retry.
