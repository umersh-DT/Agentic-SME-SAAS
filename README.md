# Agentic SME SaaS: Autonomous AI Assistant as a Service

> **Turnkey AI Employee for SMEs • Pure Cloud Multi-Tenant SaaS • OpenHuman Core • WhatsApp-Native Interface**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11+-brightgreen.svg)](https://python.org)
[![Docker: Supported](https://img.shields.io/badge/Docker-Multi--Tenant-blue)](https://docker.com)

---

## 1. Executive Summary

The traditional AI agency model—building bespoke custom bots for individual businesses—presents severe operational drag. Every custom client requires dedicated maintenance, manual integration troubleshooting, and constant handholding.

**Agentic-SME-SAAS** transitions this model into a standardized, high-margin (~80–85%) pure cloud digital product. Instead of selling complex dashboards or hardware mini-PCs, we deliver an **instant AI employee directly inside the business owner's WhatsApp**.

---

## 2. Core Architecture

The platform operates across four automated layers:

```
                  ┌─────────────────────────────────────┐
                  │    Business Owner (WhatsApp App)    │
                  │   Voice Notes & Text Interactions   │
                  └──────────────────┬──────────────────┘
                                     │ Webhook
                                     ▼
                  ┌─────────────────────────────────────┐
                  │    Layer 1: WhatsApp Cloud Gateway  │
                  │        (src/gateway/whatsapp.py)     │
                  └──────────────────┬──────────────────┘
                                     │
                                     ▼
                  ┌─────────────────────────────────────┐
                  │    Layer 2: Multi-Tenant Manager    │
                  │   (Isolated Tenant SQLite Context)  │
                  │         (data/tenants/*.db)         │
                  └──────────────────┬──────────────────┘
                                     │
                                     ▼
                  ┌─────────────────────────────────────┐
                  │     Layer 3: Pre-Bundled Skills     │
                  │  • Calendar Sync    • Invoicing     │
                  │  • Memory Tree      • Web Research  │
                  │  • Weekly SEO Manager               │
                  └──────────────────┬──────────────────┘
                                     │
                                     ▼
                  ┌─────────────────────────────────────┐
                  │ Layer 4: LLM Proxy & TokenJuice     │
                  │  (Gemini / Claude / GPT Router)     │
                  │    Context Pruning (-60% to -80%)   │
                  └─────────────────────────────────────┘
```

1. **WhatsApp-Native Gateway (`src/gateway/`):** Inbound voice notes and messages flow via Meta's official WhatsApp Cloud API directly to the tenant's agent. Zero client learning curve.
2. **Multi-Tenant Isolation (`docker/`, `data/tenants/`):** Dedicated SQLite databases for every tenant. Client A cannot see Client B's appointments, customer data, or invoices.
3. **Pre-Bundled Skills Engine (`src/skills/`):** Out-of-the-box business capabilities:
   * **Smart Calendar Sync:** Bi-directional sync with Google Calendar and Cal.com.
   * **Instant Invoicing:** Automated PDF invoice creation with integrated Stripe checkout links.
   * **Web & Supplier Research:** Headless scraping for competitive pricing and material costs.
   * **Persistent Memory Tree:** SQLite context graph remembering business pricing rules, operating policies, and preferences.
   * **Automated Weekly SEO:** Weekly domain health check, keyword position tracking, and WhatsApp performance summaries.
4. **Intelligent LLM Proxy (`src/core/proxy.py`):** Cost-optimizing proxy leveraging TokenJuice compression to cut API consumption by 60–80%, yielding sustained high margins.

---

## 3. Repository Structure

```text
Agentic-SME-SAAS/
├── .github/workflows/         # CI/CD deployment and skill testing pipelines
├── config/
│   ├── tenants.yaml           # Tenant registry and configuration
│   └── default_settings.yaml  # Default LLM proxy and compression parameters
├── data/tenants/              # Isolated per-tenant SQLite context graphs (.gitignored)
├── docker/
│   ├── Dockerfile.core        # Base OpenHuman execution runtime
│   └── docker-compose.yml     # Multi-tenant network orchestrator
├── src/
│   ├── core/                  # OpenHuman execution loop & LLM proxy
│   ├── gateway/               # WhatsApp Cloud API ingress controllers
│   ├── skills/                # Pre-bundled plugins (Calendar, Invoice, SEO, etc.)
│   └── utils/                 # Structured logging, crypto, and telemetry
├── PROJECT_STATE.md           # Master ground-truth document for AI grounding
└── ROADMAP.md                 # Project milestone execution board
```

---

## 4. Getting Started

### Prerequisites
* A Linux server with Docker & Docker Compose v2+, ports 80 and 443 open
* A domain name whose DNS `A` record points at the server (needed for HTTPS)
* A Meta developer app with WhatsApp (token, phone number ID, app secret) and a Gemini or OpenAI API key
* An email account to send alerts from (a Gmail "App password" works)

### Run the tests locally
```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t .
```

### Deploy on the server
1. Clone the repository and create the settings file:
   ```bash
   git clone https://github.com/umersh-DT/Agentic-SME-SAAS.git
   cd Agentic-SME-SAAS
   cp .env.example .env
   chmod 600 .env
   nano .env        # fill in every REQUIRED value
   ```
   * **AI model:** set `LLM_MODEL` and the matching key — e.g. `LLM_MODEL=gemini/gemini-2.5-flash` with
     `GEMINI_API_KEY`, or `LLM_MODEL=openai/gpt-4o-mini` with `OPENAI_API_KEY`. The app will not start
     if the key for the chosen model is missing.
   * **Businesses and phone numbers:** `cp config/tenants.local.example.yaml config/tenants.local.yaml`,
     put the real numbers in, and set `TENANTS_FILE=tenants.local.yaml` in `.env`. This file is never
     committed to git. Restart the app after editing it.
   * **No domain yet?** Use the free address `DOMAIN=<server-ip-with-dashes>.sslip.io`
     (e.g. `167-233-67-253.sslip.io`); Caddy gets a real HTTPS certificate for it.
2. Start the app behind Caddy (HTTPS is obtained automatically for `DOMAIN`):
   ```bash
   docker compose --env-file .env -f docker/docker-compose.yml up -d --build
   ```
   If a required value is missing, Docker stops with a message naming it.
   The app itself is not published on any host port; only Caddy listens on 80/443,
   and `/metrics` is not reachable from the internet.
3. Check it: `curl https://YOUR_DOMAIN/health` should return `"status": "healthy"`.
4. In Meta (developers.facebook.com → your app → WhatsApp → Configuration → Webhook):
   Callback URL `https://YOUR_DOMAIN/webhook/whatsapp`, Verify token = `WHATSAPP_VERIFY_TOKEN` from `.env`,
   click **Verify and save**, then subscribe to the **messages** field.
5. Send a test alert email:
   ```bash
   docker compose --env-file .env -f docker/docker-compose.yml exec gateway python -m src.utils.alerts --test
   ```

### Usage limits (free pilot)
`config/default_settings.yaml` → `quotas.enforce: false`. AI usage and cost are recorded per
business, nobody is blocked, and `ALERT_EMAIL` gets one email per business per month when its
spend passes the plan amount in `quotas_usd_monthly`. Set `enforce: true` and restart to block again.

### What people can send on WhatsApp
* Questions about the business → answered from the owner's saved rules.
* `Invoice Ali 500 AED for deep cleaning` → draft invoice (the amount includes 5% VAT); the owner receives
  it as a PDF marked DRAFT.
* Voice notes → transcribed; the reply starts with `You said: "…"`, then it is handled like typed text.
  Transcription cost is recorded per business like AI usage.
* `help` → list of what the assistant can do.
* Owner only: `Remember: …` (teach a rule), `list rules`, `change rule 2: new text`, `forget rule 2`,
  `approve invoice 1001` (sends the final PDF without DRAFT). These commands do not use the AI.

### Google Calendar bookings and SEO report (free Google services)
One-time (operator):
1. console.cloud.google.com → create a project → enable **Google Calendar API**, **Google Search Console API**
   and **PageSpeed Insights API**.
2. IAM & Admin → Service accounts → Create (no roles needed) → Keys → Add key → JSON.
3. Copy the file to the server as `config/google-service-account.json` (git-ignored) and restart.
   Its `client_email` (…@….iam.gserviceaccount.com) is the address owners share with.

Per business (owner + operator):
* **Calendar:** owner opens Google Calendar → Settings → their calendar → *Share with specific people* → adds the
  service account email with **Make changes to events**. Operator sets `google_calendar_id` (usually the owner's
  Gmail address), and optionally `timezone`, `business_hours`, `appointment_minutes` in `config/tenants.local.yaml`.
* **SEO:** operator sets `website_url`. For real Google Search numbers the owner adds the service account email as
  a user in Google Search Console, and the operator sets `search_console_property`
  (e.g. `sc-domain:example.com`).

### Managing businesses from WhatsApp (platform admin)
Set `PLATFORM_ADMIN_PHONES=+9715…` (comma-separated) in `.env` and `TENANTS_FILE=tenants.local.yaml`, and let the
app write the business list once: `chown -R 1000:1000 config`. Then from an admin number send `admin help`,
`list businesses`, `add business Ali Cleaning owner +9715…`, `add staff +9715… to Ali Cleaning`,
`set calendar owner@gmail.com for Ali Cleaning`, `remove business Ali Cleaning` … Changes apply immediately,
no restart. Removing a business keeps its saved data.

### Automatic updates
Every pull request runs the tests on GitHub. When a change reaches `main`, GitHub runs the tests and then
`scripts/deploy.sh` on the server: fast-forward to the new code, rebuild, health check; if the check fails it
returns to the previous version and emails `ALERT_EMAIL`. One-time setup on the server:
```bash
ssh-keygen -t ed25519 -N "" -C github-deploy -f ~/.ssh/github_deploy
echo "command=\"/root/Agentic-SME-SAAS/scripts/deploy.sh\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty $(cat ~/.ssh/github_deploy.pub)" >> ~/.ssh/authorized_keys
cat ~/.ssh/github_deploy          # -> GitHub secret DEPLOY_SSH_KEY
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | awk '{print $2}'   # SHA256:... -> GitHub secret DEPLOY_KNOWN_HOSTS
```
and add `DEPLOY_HOST=<server IP>` as a third secret (GitHub → Settings → Secrets and variables → Actions).
The key can only run the update script. Manual update any time: `./scripts/deploy.sh`.

---

## 5. Backups

`scripts/backup.sh` takes a consistent snapshot of every business's database (plus
`config/tenants.yaml`) from the running app, encrypts it with `BACKUP_PASSPHRASE`, checks it can be
decrypted, and copies it off the server with [rclone](https://rclone.org) to `BACKUP_REMOTE`.
If any step fails, `ALERT_EMAIL` gets an email. Remote copies are never deleted by the script;
local copies older than `BACKUP_LOCAL_KEEP_DAYS` (default 14) are removed.

### One-time setup
```bash
sudo apt install -y gnupg rclone
rclone config                  # create a remote, e.g. "gdrive" (Google Drive) or an SFTP server
# in .env: BACKUP_REMOTE=gdrive:assistant-backups and a long BACKUP_PASSPHRASE
./scripts/backup.sh            # run once by hand; it should end with "Backup OK"
crontab -e                     # add the daily schedule:
# 15 3 * * * /full/path/to/Agentic-SME-SAAS/scripts/backup.sh >> /var/log/agentic-backup.log 2>&1
```
Keep a copy of `BACKUP_PASSPHRASE` somewhere safe off the server — without it the backups cannot be opened.

### Restore
Restoring replaces the current data of every business in the backup. Run from the repository folder:
```bash
# 1. Pick a backup and download it
rclone lsf gdrive:assistant-backups
rclone copy gdrive:assistant-backups/agentic-sme-backup_YYYYMMDD_HHMMSS.tar.gz.gpg .

# 2. Decrypt and unpack (asks for BACKUP_PASSPHRASE)
mkdir restore
gpg --decrypt agentic-sme-backup_YYYYMMDD_HHMMSS.tar.gz.gpg | tar xz -C restore
ls restore/tenants             # one .sqlite file per business

# 3. Stop the app, copy the databases into the data volume, remove stale journal files
docker compose --env-file .env -f docker/docker-compose.yml stop gateway
docker compose --env-file .env -f docker/docker-compose.yml run --rm --no-deps \
  -v "$PWD/restore:/restore:ro" --entrypoint sh gateway -c \
  'rm -f /app/data/tenants/*.sqlite-wal /app/data/tenants/*.sqlite-shm && cp /restore/tenants/*.sqlite /app/data/tenants/'

# 4. Only if the business list itself was lost: cp restore/config/tenants.yaml config/tenants.yaml

# 5. Start again and check
docker compose --env-file .env -f docker/docker-compose.yml up -d
curl https://YOUR_DOMAIN/health
```
