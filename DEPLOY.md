# Remote Deployment

To validate everything on your machine with Docker first, see **[LOCAL.md](LOCAL.md)** (`bash ops/local_docker_up.sh`).

This project can be deployed to a fresh Ubuntu server with one local command.

## GCP (e.g. project `your-gcp-project`, machine type `e2-small`)

On your laptop, install [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) and sign in:

```bash
gcloud auth login your-email@example.com
gcloud config set project your-gcp-project
gcloud config set compute/zone us-central1-a
```

Enable billing and APIs on the project (Compute Engine API), then create the VM:

```bash
bash ops/gcp_create_vm.sh
# or pick zone explicitly:
# GCP_ZONE=europe-west1-b bash ops/gcp_create_vm.sh
```

SSH in (first time `gcloud` adds your key):

```bash
gcloud compute ssh your-nanoclaw-vm --zone=us-central1-a
```

On the VM, **clone this repo once** (or set `NANOCLAW_GIT_REMOTE` on first deploy so the script clones for you):

```bash
sudo mkdir -p /opt && sudo git clone https://github.com/YOU/py-nanoclaw.git /opt/nanoclaw
sudo chown -R "$USER:$USER" /opt/nanoclaw
```

Then from your laptop, deploy (replace IP with the VM external IP from `gcp_create_vm.sh` output). Code updates are **`git pull` on the server**, not rsync:

```bash
bash ops/deploy_remote.sh "$(whoami)@EXTERNAL_IP" /opt/nanoclaw
```

Optional: first-time clone from your laptop without SSH git setup:

```bash
NANOCLAW_GIT_REMOTE=https://github.com/YOU/py-nanoclaw.git bash ops/deploy_remote.sh user@EXTERNAL_IP /opt/nanoclaw
```

Branch defaults to `main`; override with `NANOCLAW_GIT_BRANCH=master`. Legacy rsync: `DEPLOY_SYNC=rsync`. After the first run, edit `.env` on the server if the script created it from `.env.example`, then rerun the deploy command.

On the server, confirm `NANOCLAW_DOCKER_NETWORK` matches the isolated agent network (see `.env.example`; default `nanoclaw_agent`). The compose default bridge is usually `nanoclaw_default` for the bot and socket proxy:

```bash
sudo docker network ls
```

## 1) First-time setup + deploy

Ensure the server has a git checkout at the app path (see GCP section above), then from your local machine:

```bash
bash ops/deploy_remote.sh user@your-server /opt/nanoclaw
```

The script will:
- update code on the remote with `git pull` (or clone if `NANOCLAW_GIT_REMOTE` is set)
- install Docker + Compose plugin on remote
- create runtime folders
- create `.env` from `.env.example` if missing
- start compose (bot, OneCLI, docker-socket-proxy)

Security note:
- Bot containers do **not** mount the host Docker socket directly.
- A `docker-socket-proxy` sidecar is used so bot only talks to a restricted Docker API endpoint.

If `.env` is created for the first time, fill it on remote and rerun the deploy command.

## 2) Remote `.env` requirements

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_USER_ID`
- `OPENAI_API_KEY`
- `ONECLI_URL` (defaults to `http://onecli:10255` in compose — use this value for Docker; only change if you run OneCLI elsewhere)
- `ONECLI_API_KEY` — **required after OneCLI onboarding** (dashboard API key used for `/api/container-config`; see §3)
- `NANOCLAW_ONECLI_API_URL` (optional override; default derives from `ONECLI_URL` by swapping `:10255` to `:10254`)
- `ONECLI_DB_PASSWORD` (optional; defaults to `onecli` — must match Postgres and stay URL-safe in `DATABASE_URL`)

## 3) OneCLI setup (dashboard + `.env`)

The bot talks to the OneCLI API to fetch SDK-style container config, then applies that env/CA config to spawned agent containers. You must create OneCLI credentials/secrets once and put the API key in `.env`.

### Local (Docker on your laptop)

1. Start the stack (`bash ops/local_docker_up.sh` or `docker compose up -d`).
2. Wait until Postgres and OneCLI are healthy (`docker compose ps`).
3. Open **http://127.0.0.1:10254** (dashboard). Compose binds these ports to **loopback** only.
4. In the UI: copy your OneCLI **API key** (`oc_…`) and put it in `.env` as `ONECLI_API_KEY`.
5. Add an **Anthropic**-type secret in OneCLI for **api.anthropic.com** (see [OneCLI](https://github.com/onecli/onecli)). Nanoclaw sends a **placeholder** API key through the gateway (default `placeholder`; set `NANOCLAW_ANTHROPIC_PLACEHOLDER_KEY` if your rules need another value); OneCLI injects the real key for outbound requests.
6. In the repo **`.env`** on the machine that runs Compose, set:
   - `ONECLI_URL=http://onecli:10255`
   - `ONECLI_API_KEY=<paste the OneCLI API key>`
7. Recreate the bot so it picks up env: `docker compose up -d bot` (or `docker compose up -d`).

Nanoclaw fetches OneCLI’s SDK container-config (`/api/container-config`) and applies the returned proxy env + CA mount to each agent run. This matches the OneCLI SDK flow ([Node SDK docs](https://www.onecli.sh/docs/sdks/node)).

### Remote server (SSH tunnel)

The dashboard listens on **127.0.0.1** on the server, so your browser cannot reach it by public IP. From your **laptop**:

```bash
ssh -L 10254:127.0.0.1:10254 -L 10255:127.0.0.1:10255 youruser@REMOTE_IP
```

Keep that session open, then open **http://localhost:10254** on the laptop. Complete the same steps (API key + Anthropic secret), then put `ONECLI_URL` and `ONECLI_API_KEY` into **`.env` on the server** (e.g. `/opt/nanoclaw/.env`), and restart the stack there:

```bash
cd /opt/nanoclaw && sudo docker compose up -d
```

Do not publish `10254`/`10255` on `0.0.0.0` on the public internet without proper authentication and TLS.

### Optional: OneCLI installable CLI

If you use the standalone `onecli` CLI on a host (not required for the Docker dashboard flow):

```bash
curl -fsSL onecli.sh/install | sh
```

Example secret creation via CLI (when supported by your version):

```bash
onecli secrets create \
  --name Anthropic \
  --type anthropic \
  --value "sk-ant-..." \
  --host-pattern api.anthropic.com
```

## 4) Useful remote commands

```bash
cd /opt/nanoclaw
sudo docker compose ps
sudo docker compose logs -f bot
sudo docker compose logs -f onecli
```
