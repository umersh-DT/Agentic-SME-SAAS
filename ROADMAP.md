# Product & Technical Roadmap: Agentic-SME-SAAS

## Phase 1: Architecture & Tenant Data Isolation (COMPLETED)
- [x] Multi-tenant storage architecture using isolated SQLite databases (`data/tenants/{tenant_id}.sqlite`) in WAL mode.
- [x] Hierarchical Persistent Memory Tree (`src/skills/memory_tree.py`) with FTS5 virtual indexing and BM25 ranking.
- [x] Base container configuration (`docker/Dockerfile.core`) and compose stack (`docker/docker-compose.yml`).

---

## Phase 2: Ingress Gateway & Core Logic Prototype (COMPLETED)
- [x] Invoicing math and estimate drafting engine (`src/skills/invoicing.py`).
- [x] Twilio WhatsApp webhook with HMAC-SHA1 signature verification and deduplication cache.
- [x] Stripe billing webhook handler with HMAC-SHA256 verification and replay attack tolerance.
- [x] Automated SQLite backup daemon using the SQLite Online Backup API (`src/utils/backup_daemon.py`).
- [x] Outbound WhatsApp reply client via Twilio REST API (`src/skills/whatsapp_reply.py`).

---

## Remediation Roadmap: Production Working Loop

### Stage 1: Documentation Baseline & Security Hardening (COMPLETED)
- [x] Realign `PROJECT_STATE.md` and `ROADMAP.md` to reflect verified working code.
- [x] Enforce fail-closed authentication on Twilio and Stripe webhooks (reject requests if secrets are unset).
- [x] Add `STRIPE_WEBHOOK_SECRET` and `GRAFANA_ADMIN_PASSWORD` to `.env.example`.
- [x] Sanitize `tenant_id` with regex (`^tenant_[a-z0-9_]+$`) to prevent directory traversal in file provisioning.
- [x] Remove public host port bindings for Prometheus (9090) and Grafana (3000); block `/metrics` in Nginx.
- [x] Eliminate hardcoded `tenant_curtains_001` fallback and fix `tenant_id` vs `id` directory lookup.

### Stage 2: Single Tenant Registry & Routing Engine (NEXT)
- [ ] Unify tenant schema (`TenantConfig`) across `config/tenants.yaml`, `src/utils/security.py`, and provisioning.
- [ ] Route exclusively by sender phone (`From`) and enforce strict one-to-one phone-to-tenant mapping.
- [ ] Return polite "Sender not registered" response for unknown phone numbers without touching tenant storage.
- [ ] Remove `config/tenants_pilot.yaml` and standardize on single configuration registry.

### Stage 3: End-to-End Agent Execution Loop
- [ ] Build `src/core/agent.py` using LiteLLM for structured tool calling and per-tenant token cost metering.
- [ ] Wire `src/gateway/twilio_webhook.py` $\to$ FastAPI `BackgroundTasks` $\to$ `dispatcher.py`.
- [ ] Connect agent output directly to `WhatsAppReplySkill` for automated outbound responses.
- [ ] Configure Uvicorn `--proxy-headers --forwarded-allow-ips=*` for production TLS signature validation.
- [ ] End-to-end integration test: Owner WhatsApp message $\to$ Tool invocation (Invoice) $\to$ WhatsApp response.

### Stage 4: High-Value Skills, Billing Lifecycle & Hermetic CI
- [ ] Invoicing enhancements: Binary PDF generation (`reportlab`), persistent invoice numbers, and Stripe Payment Links.
- [ ] Inbound voice note transcription via Twilio media attachments (`MediaUrl0`).
- [ ] Full Stripe lifecycle: Checkout session creation, welcome template dispatch, and cancellation suspension.
- [ ] Hermetic unit tests (using ephemeral `tempfile` directories) and GitHub Actions CI workflow.