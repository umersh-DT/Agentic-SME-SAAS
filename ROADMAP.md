# Product & Technical Roadmap: Agentic-SME-SAAS

## Phase 1: Architecture, Scaffolding & Infrastructure (COMPLETED)
- [x] Define business model, unit economics, and launch pricing tiers ($49/mo Starter vs $89/mo Pro)
- [x] Finalize technical architecture (Docker multi-tenant isolation, OpenHuman Core, WhatsApp gateway)
- [x] Define and document core SME validation personas (Curtains/Installation, Freelance Photographer, Home Cleaning)
- [x] Define automated Weekly SEO Management skill module specifications
- [x] Initialize Git repository (`umersh-DT/Agentic-SME-SAAS`) with complete directory tree
- [x] Generate SSOT grounding documentation (`PROJECT_STATE.md`, `README.md`, `ROADMAP.md`)
- [x] Implement `docker/Dockerfile.core` base runtime image
- [x] Implement production-ready `docker/docker-compose.yml` with health checks, network mesh, and volume mounts
- [x] Create `config/tenants.yaml` schema with Pydantic validation suite (`src/utils/security.py`)
- [x] Provision staging cloud VPS (Hetzner CPX21 `167.233.67.253`, Ubuntu 24.04 LTS, Docker stack verified with 3 worker instances)

---

## Phase 2: OpenHuman Core & Pre-Bundled Skills (ACTIVE)
- [ ] Implement SQLite Persistent Memory Tree (`src/skills/memory_tree.py`) with FTS5 search and hierarchical nodes
- [ ] Build OpenHuman context extractor to parse incoming WhatsApp rules and populate memory nodes
- [ ] Implement Google Calendar scheduling skill module (`src/skills/calendar_sync.py`)
- [ ] Implement automated invoice and estimate drafting skill module (`src/skills/invoicing.py`)
- [ ] Implement deep-search & competitor research skill module (`src/skills/research.py`)
- [ ] Implement automated Weekly SEO Management skill module (`src/skills/seo_manager.py`)
- [ ] Build end-to-end integration test suite simulating multi-tenant message flows and tool execution

---

## Phase 3: WhatsApp Gateway, Twilio & Stripe Billing
- [ ] Twilio WhatsApp Business API Webhook integration
- [ ] Tenant signature authentication & message rate-limiting
- [ ] Stripe customer portal & recurring subscription billing integration
- [ ] Automated tenant provisioning and volume mount setup upon subscription checkout

---

## Phase 4: Production Hardening, Monitoring & Launch
- [ ] Automated daily backup daemon for tenant SQLite databases to S3/Cloud Storage
- [ ] Prometheus metrics and Grafana observability dashboard
- [ ] Domain configuration, Nginx reverse proxy, and Let's Encrypt SSL automation
- [ ] Production onboarding of initial 3 pilot SMEs