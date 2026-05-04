# Day-2 Deployment

For first-time setup of a fresh VM, see **[BOOTSTRAP.md](BOOTSTRAP.md)**.
For local Docker development, see **[LOCAL.md](LOCAL.md)**.

> **Hardened deploy path:** the production VM (`nanoclaw` in `ironclaw-assistant`,
> zone `europe-west1-b`) accepts SSH only through Google IAP — port 22 is closed
> to the public internet. The GitHub Actions auto-deploy (`appleboy/ssh-action`)
> can't reach the VM and is gated behind `workflow_dispatch`. Deploys are run
> manually from a developer laptop using IAP-tunneled `gcloud compute ssh`.

## Standard deploy (full rebuild)

```bash
gcloud compute ssh nanoclaw --tunnel-through-iap \
    --zone=europe-west1-b --project=ironclaw-assistant \
    --command='cd ~/nanoclaw && git pull && sudo docker compose build && sudo docker compose up -d'
```

## Faster: code-only deploy (no Dockerfile / compose changes)

```bash
gcloud compute ssh nanoclaw --tunnel-through-iap \
    --zone=europe-west1-b --project=ironclaw-assistant \
    --command='cd ~/nanoclaw && git pull && sudo docker compose build bot agent && sudo docker compose up -d bot agent'
```

## Compose-only deploy (`docker-compose.yml` / `.env.example` changed, no code)

```bash
gcloud compute ssh nanoclaw --tunnel-through-iap \
    --zone=europe-west1-b --project=ironclaw-assistant \
    --command='cd ~/nanoclaw && git pull && sudo docker compose up -d'
```

`.env` on the server is gitignored and untouched by `git pull`.

## Switching agent auth: OAuth (subscription) ↔ API key

Claude Code CLI picks auth from env vars and **prefers `ANTHROPIC_API_KEY` over
`CLAUDE_CODE_OAUTH_TOKEN`** when both are set, so exactly one must be active in
`docker-compose.yml` (agent service).

**To use OAuth (Claude Pro/Max subscription):**
1. In `docker-compose.yml` agent env: uncomment `CLAUDE_CODE_OAUTH_TOKEN: placeholder`,
   comment out `ANTHROPIC_API_KEY: placeholder`.
2. In OneCLI dashboard (tunnel below): enable the OAuth secret, disable the API key secret.
3. Deploy via the standard command above; then `sudo docker compose up -d agent` to pick
   up env changes.

**To use API key (fallback):** flip both — and re-deploy.

**Get a fresh OAuth token:** run `claude setup-token` on your laptop (opens browser,
prints `sk-ant-oat…` to paste into OneCLI).

**Verify which auth path the CLI is actually using** (from the VM):

```bash
sudo docker compose exec -T agent env | grep -i -E "claude_code_oauth|anthropic_api"
sudo docker compose exec -T agent /usr/local/lib/python3.13/site-packages/claude_agent_sdk/_bundled/claude --print --output-format json --model sonnet hi
```

A 401 "Invalid API key" on the second command means the env var in the container doesn't
match the secret type currently enabled in OneCLI.

## OneCLI dashboard tunnel

```bash
gcloud compute ssh nanoclaw --tunnel-through-iap \
    --zone=europe-west1-b --project=ironclaw-assistant \
    -- -L 10254:localhost:10254 -N
# leave running, open http://localhost:10254 in your browser
```

## Useful remote commands

```bash
# Open an interactive shell on the VM
gcloud compute ssh nanoclaw --tunnel-through-iap \
    --zone=europe-west1-b --project=ironclaw-assistant

# Service status
cd ~/nanoclaw && sudo docker compose ps

# Live logs
sudo docker compose logs -f                # all services
sudo docker compose logs -f bot agent      # specific services

# Restart one service
sudo docker compose restart bot

# Full restart
sudo docker compose down && sudo docker compose up -d
```

## Switching gcloud accounts (if you run multiple)

```bash
# One-off override per command:
gcloud compute ssh nanoclaw --tunnel-through-iap \
    --zone=europe-west1-b --project=ironclaw-assistant \
    --account=samilyak@gmail.com

# Or named gcloud configurations (recommended for two-account workflows):
gcloud config configurations create personal
gcloud config set account samilyak@gmail.com
gcloud config set project ironclaw-assistant
gcloud config set compute/zone europe-west1-b
gcloud config configurations activate personal   # flip to this profile
gcloud config configurations activate default    # flip back
gcloud config configurations list
```

## Updating Google Calendar credentials

The Google OAuth refresh tokens live in `~/nanoclaw/secrets/google-oauth-creds.json`
on the VM (mode `0600`, owned by UID 10010 — the broker), only mounted into the
`creds-broker` sidecar. To rotate or add an account, regenerate locally and `scp`:

```bash
# on your laptop
.venv/bin/python ops/google_oauth_bootstrap.py \
    --account work_admin --client-secrets path/to/client_secret_*.json \
    --creds /tmp/.nanoclaw_google_creds.json
gcloud compute scp --tunnel-through-iap \
    --zone=europe-west1-b --project=ironclaw-assistant \
    /tmp/.nanoclaw_google_creds.json \
    nanoclaw:/tmp/google-oauth-creds.json
gcloud compute ssh nanoclaw --tunnel-through-iap --zone=europe-west1-b --project=ironclaw-assistant \
    --command='sudo mv /tmp/google-oauth-creds.json ~/nanoclaw/secrets/google-oauth-creds.json && sudo chown 10010:10010 ~/nanoclaw/secrets/google-oauth-creds.json && sudo chmod 600 ~/nanoclaw/secrets/google-oauth-creds.json'
```

Broker reloads the file on every request — no restart needed.

## Migrating from `.nanoclaw_google_creds.json` to the broker (Tier 1.3)

If you're upgrading a VM that pre-dates Tier 1.3:

```bash
gcloud compute ssh nanoclaw --tunnel-through-iap \
    --zone=europe-west1-b --project=ironclaw-assistant --command='set -e
cd ~/nanoclaw
sudo mkdir -p secrets
sudo chown 10010:10010 secrets
sudo chmod 700 secrets
# Move existing google creds (if present) into the broker store
[ -f runtime/sessions/.nanoclaw_google_creds.json ] && \
    sudo mv runtime/sessions/.nanoclaw_google_creds.json secrets/google-oauth-creds.json
# Pull the Telegram bot token out of .env into a dedicated file
grep "^TELEGRAM_BOT_TOKEN=" .env | cut -d= -f2- | sudo tee secrets/telegram-bot-token > /dev/null
# Lock everything down
sudo chown 10010:10010 secrets/*
sudo chmod 600 secrets/*
ls -la secrets/'
```

After verifying the broker works (logs show `broker listening on /run/...`),
remove `TELEGRAM_BOT_TOKEN=…` from `~/nanoclaw/.env`.
