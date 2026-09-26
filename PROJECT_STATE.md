# Project State: Agentic-SME-SAAS

**Current Status:** Remediation Stage 1 Complete (Baseline Reset & Security Hardening)  
**Last Updated:** September 24, 2026  
**Repository:** `https://github.com/umersh-DT/Agentic-SME-SAAS`

---

## 1. Verified Working Modules
- `src/skills/memory_tree.py`: WAL-mode SQLite hierarchical memory tree with FTS5 search (per-tenant file isolation).
- `src/skills/invoicing.py`: Quotation math engine (line items, VAT calculations, deposit splits, WhatsApp message formatting).
- `src/gateway/main.py`: FastAPI gateway entrypoint with `/health` and metric middleware.
- `src/gateway/twilio_webhook.py`: Twilio WhatsApp webhook with HMAC-SHA1 validation, 10-minute sliding-window deduplication, and fail-closed authentication.
- `src/gateway/stripe_billing.py`: Stripe webhook handler with HMAC-SHA256 signature verification, fail-closed authentication, and tenant ID path sanitization.
- `src/skills/whatsapp_reply.py`: Async client for outbound WhatsApp messaging via Twilio REST API with exponential backoff.
- `src/utils/backup_daemon.py`: SQLite Online Backup API snapshot engine for WAL-mode tenant databases with local retention pruning.

---

## 2. Incomplete or Stubbed Components
- **Autonomous Agent Loop:** Webhook does not yet invoke `dispatcher.process_incoming_message` via `BackgroundTasks`. No LLM router exists yet (`src/core/agent.py` to be built in Stage 3).
- **Calendar (`calendar_sync.py`):** In-memory event list only. Live Google Calendar / Cal.com synchronization is not implemented.
- **Competitor Research (`research.py`):** Returns static sample data. Search API integration is not implemented.
- **Invoicing PDF & Payments:** ReportLab PDF compilation and live Stripe Payment Links are not implemented.
- **Voice Notes:** Webhook does not yet parse `MediaUrl0` or perform speech-to-text transcription.

---

## 3. Infrastructure & Deployment
- **Staging VPS:** Hetzner Cloud (`167.233.67.253`), Ubuntu 24.04 LTS.
- **Architecture Model:** Single shared gateway/worker container using isolated SQLite databases per tenant (`data/tenants/{tenant_id}.sqlite`).
- **Telemetry & Edge:** Prometheus and Grafana restricted to internal Docker network; `/metrics` blocked from public ingress in Nginx.

## Current Status: Stage 2 Remediated (Pending Re-Review Approval)
- Unified TenantConfig Pydantic model with strict E.164 (`^\+[1-9]\d{1,14}$`) validation.
- Stripe billing webhook provisions SQLite asynchronously and registers tenant into `config/tenants.yaml`.
- Stripe provisioning failures return HTTP 500 to preserve Stripe webhook retry semantics.
- Canned customer autoreply removed from `dispatcher.py`.
- Rate-limited graceful rejection for unregistered senders via TwiML.
- Unit test suite expanded to 19 hermetic tests covering unmocked directory routing, collision rejection, and isolated temporary directories.