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

# Roadmap: Agentic SME SaaS

## Stage 1: Documentation Reset & Security Hardening
- [x] Canonical documentation reset
- [x] Security audit & hermetic tests
- [x] Staging deployment (Approved - d7a7488, ab71b3c)

## Stage 2: Tenant Registry & Ingress Routing
- [x] Single TenantConfig Pydantic Schema & Strict E.164 (`^\+[1-9]\d{6,14}$`)
- [x] Single canonical `config/tenants.yaml`
- [x] Sender-only deterministic 1-to-1 phone routing
- [x] Phone collision protection (existing paying tenants protected from checkout takeovers)
- [x] In-memory TenantConfig pre-validation before atomic YAML write
- [x] Non-retryable metadata defects return 200; operational failures return 500
- [x] Daily rate-limited unmapped sender rejection TwiML
- [x] Hermetic automated test suite (21/21 passing, clean unmocked directory tests)
- **Status:** Complete & Remediated (Ready for Final Sign-Off)

## Stage 3: Agentic Core & Business Skill Pipeline [NEXT]
- [ ] LiteLLM integration with single model provider
- [ ] Per-tenant token metering and usage limits
- [ ] Context extractor & dynamic tool calling
- [ ] Memory tree FTS5 search & CRM state tracking
- [ ] Uvicorn reverse-proxy headers (`--proxy-headers --forwarded-allow-ips=*`)