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

1. **WhatsApp-Native Gateway (`src/gateway/`):** Inbound voice notes and messages flow via Twilio/WhatsApp Cloud API directly to the tenant's agent. Zero client learning curve.
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
│   ├── gateway/               # WhatsApp / Twilio ingress controllers
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
* Twilio WhatsApp credentials and an OpenAI API key
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
2. Start the app behind Caddy (HTTPS is obtained automatically for `DOMAIN`):
   ```bash
   docker compose --env-file .env -f docker/docker-compose.yml up -d --build
   ```
   If a required value is missing, Docker stops with a message naming it.
   The app itself is not published on any host port; only Caddy listens on 80/443,
   and `/metrics` is not reachable from the internet.
3. Check it: `curl https://YOUR_DOMAIN/health` should return `"status": "healthy"`.
4. In Twilio, set the WhatsApp inbound webhook to `https://YOUR_DOMAIN/webhook/whatsapp` (POST).
5. Send a test alert email:
   ```bash
   docker compose --env-file .env -f docker/docker-compose.yml exec gateway python -m src.utils.alerts --test
   ```

### Usage limits (free pilot)
`config/default_settings.yaml` → `quotas.enforce: false`. AI usage and cost are recorded per
business, nobody is blocked, and `ALERT_EMAIL` gets one email per business per month when its
spend passes the plan amount in `quotas_usd_monthly`. Set `enforce: true` and restart to block again.

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
