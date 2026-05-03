# Bootstrap — hardened from-scratch setup

This guide walks through standing up py-nanoclaw on a fresh GCP project with a security
posture that's safe for an agent that holds OAuth tokens to multiple Google accounts and
(soon) full Gmail access. Day-2 operations live in [DEPLOY.md](DEPLOY.md). For local
Docker development, see [LOCAL.md](LOCAL.md).

The hardened defaults applied here are tracked as **Tier 0** in the security plan. Tier 1
(per-container UIDs, creds-broker sidecar, container hardening, egress allowlist) is
mandatory before deploying Gmail MCP — see the security plan in `~/.claude/plans/`.

---

## 0. Prerequisites

On your laptop:

- [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) installed (`gcloud`).
- A GCP project with billing enabled. Replace `your-gcp-project` everywhere below.
- A Google account that owns / has Owner on that project.

```bash
gcloud auth login YOU@example.com
gcloud config set project your-gcp-project
gcloud config set compute/zone europe-west1-b
gcloud services enable compute.googleapis.com iap.googleapis.com calendar-json.googleapis.com
```

If you have multiple accounts (work + personal), see DEPLOY.md → "Switching gcloud accounts".

## 1. Create the hardened VM

```bash
GCP_PROJECT=your-gcp-project bash ops/gcp_create_vm.sh
```

The script provisions an `e2-small` Ubuntu 22.04 instance with:

- Shielded VM enabled (Secure Boot + vTPM + Integrity Monitoring)
- Service account scopes restricted to `logging.write` + `monitoring.write` only
- Tag `iap-ssh` and a firewall rule (`allow-ssh-from-iap`) allowing port 22
  only from Google IAP's source range `35.235.240.0/20`. **No public SSH.**

It also warns if the GCP default `default-allow-ssh` (0.0.0.0/0) rule still exists in your
project; delete that rule once IAP SSH is confirmed working (step 2).

## 2. Grant IAM + smoke-test SSH

```bash
# Required to use IAP TCP forwarding to SSH
gcloud projects add-iam-policy-binding your-gcp-project \
    --member=user:YOU@example.com --role=roles/iap.tunnelResourceAccessor

# Recommended: replace metadata SSH keys with IAM-managed OS Login
gcloud compute project-info add-metadata --project=your-gcp-project --metadata=enable-oslogin=TRUE
gcloud projects add-iam-policy-binding your-gcp-project \
    --member=user:YOU@example.com --role=roles/compute.osLogin

# Smoke test — must succeed BEFORE you delete any leftover public SSH rule
gcloud compute ssh nanoclaw --tunnel-through-iap \
    --zone=europe-west1-b --project=your-gcp-project --command='echo IAP-OK'
```

If the smoke test fails, do **not** delete `default-allow-ssh`. Investigate first
(`gcloud compute ssh nanoclaw --troubleshoot --tunnel-through-iap`).

## 3. Install Docker on the VM

The first time you SSH in, run the bootstrap script that installs Docker + Compose:

```bash
gcloud compute ssh nanoclaw --tunnel-through-iap --zone=europe-west1-b --project=your-gcp-project
# on the VM:
sudo bash -s < <(curl -fsSL https://raw.githubusercontent.com/YOU/py-nanoclaw/main/ops/remote_setup.sh)
```

(Or `scp` `ops/remote_setup.sh` to the VM via `gcloud compute scp --tunnel-through-iap` and run it.)

## 4. Clone the repo on the VM

```bash
# on the VM
git clone https://github.com/YOU/py-nanoclaw.git ~/nanoclaw
cd ~/nanoclaw
mkdir -p runtime/sessions runtime/media data/user-context
chmod 700 runtime/sessions
```

## 5. Configure `.env`

Create `~/nanoclaw/.env` on the VM. Required fields:

| Var | Source | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather | Bot's API token |
| `TELEGRAM_USER_ID` | your Telegram numeric ID | Bot accepts inbound only from this ID |
| `OPENAI_API_KEY` | platform.openai.com | For voice transcription (Whisper) |
| `ONECLI_URL` | `http://onecli:10255` | Internal Docker DNS — leave as default |
| `ONECLI_API_KEY` | OneCLI dashboard, see step 6 | Set after first OneCLI start |
| `ONECLI_DB_PASSWORD` | choose one | URL-safe; defaults to `onecli` |
| `GITHUB_REPO_URL` | optional | If `data/user-context/` is git-synced |
| `USER_CONTEXT_SUBPATH` | optional | Subdir of user-context git repo to mount as `/work` |
| `GITHUB_PAT` | optional | Required only if `git-sync` pushes to GitHub via HTTPS |

Lock down the file:

```bash
chmod 600 ~/nanoclaw/.env
```

## 6. OneCLI: dashboard + secrets

OneCLI provides per-host MITM credential injection (Anthropic API, etc.) and is the
project's secret vault. Bring up just the OneCLI + Postgres services first so you can
configure them before the bot/agent start needing them:

```bash
# on the VM
cd ~/nanoclaw
sudo docker compose up -d postgres onecli
```

OneCLI binds its dashboard to `127.0.0.1:10254` only — never expose it publicly. Tunnel it
through IAP from your laptop:

```bash
# on your laptop
gcloud compute ssh nanoclaw --tunnel-through-iap \
    --zone=europe-west1-b --project=your-gcp-project \
    -- -L 10254:localhost:10254 -N
# leave this running, then open http://localhost:10254 in your browser
```

In the dashboard:

1. Copy the dashboard API key (`oc_…`) and put it in `~/nanoclaw/.env` as `ONECLI_API_KEY`.
2. Add an **Anthropic** secret targeting `api.anthropic.com`. Either an OAuth token (Claude
   Pro/Max subscription, paste the `sk-ant-oat…` from `claude setup-token` on your laptop)
   or an API key (`sk-ant-api…`). Enable exactly one — see DEPLOY.md → "Switching agent
   auth" for the toggle procedure.

## 7. (Optional, for the calendar feature) bootstrap Google OAuth tokens

If the calendar MCP is enabled (it is by default in this repo), run the OAuth bootstrap
on your **laptop** once per Google account you want to connect:

```bash
# on your laptop, in the repo root
.venv/bin/python ops/google_oauth_bootstrap.py \
    --account personal \
    --client-secrets path/to/client_secret_*.json \
    --creds runtime/sessions/.nanoclaw_google_creds.json
# repeat with --account work_admin and --account work_corp
```

The Desktop OAuth client must be created in your GCP project under
**APIs & Services → Credentials**, with `*.googleapis.com/auth/calendar.events`,
`*.googleapis.com/auth/calendar.calendarlist.readonly`, and
`*.googleapis.com/auth/calendar.freebusy` scopes added on the consent screen and your
target accounts listed as test users (see [Google OAuth verification docs](https://support.google.com/cloud/answer/15549257)).

Copy the resulting creds file to the VM:

```bash
gcloud compute scp --tunnel-through-iap \
    --zone=europe-west1-b --project=your-gcp-project \
    runtime/sessions/.nanoclaw_google_creds.json \
    nanoclaw:nanoclaw/runtime/sessions/.nanoclaw_google_creds.json
```

Verify perms on the VM (must stay 0600):

```bash
gcloud compute ssh nanoclaw --tunnel-through-iap \
    --zone=europe-west1-b --project=your-gcp-project \
    --command='ls -la ~/nanoclaw/runtime/sessions/.nanoclaw_google_creds.json'
```

## 8. First deploy

```bash
gcloud compute ssh nanoclaw --tunnel-through-iap \
    --zone=europe-west1-b --project=your-gcp-project \
    --command='cd ~/nanoclaw && sudo docker compose build && sudo docker compose up -d'
```

## 9. Verify

```bash
# All five services should be Up:
gcloud compute ssh nanoclaw --tunnel-through-iap \
    --zone=europe-west1-b --project=your-gcp-project \
    --command='cd ~/nanoclaw && sudo docker compose ps'
```

Expected services: `bot`, `agent`, `onecli`, `postgres`, `git-sync`. Then send a Telegram
message to your bot (from the user ID in `TELEGRAM_USER_ID`) — it should reach the agent
and respond.

For the calendar feature, ask the agent something like *"What's on my personal calendar
tomorrow?"* — the agent should call `mcp__calendar__list_events` and answer from real data.

---

## What's still on the to-do list (Tier 1, before Gmail)

Even with Tier 0 done, the agent container can still:
- read the Google credentials file directly (same UID as the bot, even at 0600)
- write arbitrary files into `/work` and the agent could in theory exfiltrate via the
  user-context git repo
- reach any HTTPS endpoint on the internet (egress is unrestricted)

Those gaps are addressed by Tier 1 (compartmentalized UIDs, a creds-broker sidecar,
container hardening, OneCLI egress allowlist). **Do not enable the Gmail MCP until Tier 1
has landed.** See the planning notes in `~/.claude/plans/` for details.
