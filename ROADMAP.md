# Autonomous AI SME SaaS - Master Execution Roadmap

## 🎯 Target Milestones Overview
* **Target Delivery:** High-Margin (~80-85%) Pure Cloud Digital SaaS.
* **Core Framework:** OpenHuman Engine with Pre-bundled Skills.
* **Interface:** WhatsApp Cloud API / Twilio Native.

---

## 📋 Phase 1: Architecture, Scaffolding & Infrastructure (CURRENT)
- [x] Define business model, unit economics, and launch pricing tiers ($49/mo Starter vs $89/mo Pro)
- [x] Finalize technical architecture (Docker multi-tenant isolation, OpenHuman Core, WhatsApp gateway)
- [x] Define and document core SME validation personas (Curtains/Installation, Freelance Photographer, Home Cleaning)
- [x] Define automated Weekly SEO Management skill module specifications
- [x] Initialize Git repository (`umersh-DT/Agentic-SME-SAAS`) with complete directory tree
- [x] Generate SSOT grounding documentation (`PROJECT_STATE.md`, `README.md`, `ROADMAP.md`)
- [ ] Implement `docker/Dockerfile.core` base runtime image
- [ ] Implement production-ready `docker/docker-compose.yml` with health checks and volume mounts
- [ ] Create `config/tenants.yaml` schema with Pydantic validation
- [ ] Provision staging cloud VPS (Hetzner / DigitalOcean)

---

## 📋 Phase 2: OpenHuman Core & Pre-Bundled Skills
- [ ] **Memory Tree (`src/skills/memory_tree.py`):**
  - [ ] Implement SQLite schema for persistent context, client preferences, and operational rules
  - [ ] Build key-value extraction and semantic retrieval helpers
- [ ] **Calendar Module (`src/skills/calendar_sync.py`):**
  - [ ] Implement Google Calendar API OAuth2 & service account client
  - [ ] Implement Cal.com booking webhook listener and conflict resolution
- [ ] **Invoicing Module (`src/skills/invoicing.py`):**
  - [ ] Implement ReportLab / WeasyPrint PDF invoice layout generator
  - [ ] Integrate Stripe API payment link generation
- [ ] **Research Module (`src/skills/research.py`):**
  - [ ] Implement headless web search and supplier price extraction
- [ ] **Weekly SEO Manager (`src/skills/seo_manager.py`):**
  - [ ] Implement automated weekly site audits (broken links, meta tags, page performance)
  - [ ] Implement local keyword rank tracking
  - [ ] Build automated Monday morning WhatsApp report digest formatter

---

## 📋 Phase 3: Communication Gateway & LLM Proxy
- [ ] **LLM Proxy & TokenJuice (`src/core/proxy.py`):**
  - [ ] Build multi-provider router (Gemini 1.5 Flash default, Claude 3.5 Sonnet escalation)
  - [ ] Implement prompt token pruning and context compression algorithms (60-80% savings)
- [ ] **WhatsApp Gateway (`src/gateway/whatsapp.py`):**
  - [ ] Implement Twilio / Meta WhatsApp Cloud API webhook receiver
  - [ ] Add inbound voice note transcription pipeline (Whisper / Gemini multimodal audio)
  - [ ] Build tenant routing resolver based on sender phone number

---

## 📋 Phase 4: Billing, Security & Pilot Launch
- [ ] Implement Stripe webhook listener for automatic tenant container provisioning
- [ ] Add rate limiting, abuse detection, and tenant isolation unit tests
- [ ] Set up GitHub Actions CI/CD pipeline (`.github/workflows/`)
- [ ] Deploy pilot release with 3 trial SME customers