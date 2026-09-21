# Project Grounding Anchor & System State (PROJECT_STATE.md)
*Single Source of Truth (SSOT) to prevent AI drift, context loss, and hallucinations across development sessions.*

---

## 1. System Identity & Mission
* **Product:** Instant AI Employee for Small & Medium Enterprises (SMEs).
* **Model:** Pure Cloud Multi-Tenant SaaS (Zero client hardware, zero local mini-PCs, zero complex dashboards).
* **Core Engine:** OpenHuman Core (reasoning loop, token pruning, persistent context).
* **Primary Client Interface:** WhatsApp Native (WhatsApp Cloud API / Twilio gateway; voice notes and text).
* **Target Margin:** 80% to 85% gross recurring margin ($49/mo Starter, $89/mo Pro).
* **Repository:** `umersh-DT/Agentic-SME-SAAS`

---

## 2. Fixed Architectural Tenets (Do Not Violate)
1. **Strict Multi-Tenant Isolation:** 
   * Each business client has an isolated data boundary. Client SQLite databases live in `data/tenants/{tenant_id}.db`. Client A never accesses Client B's records, invoices, or calendar tokens.
2. **Centralized Inference & Token Optimization:**
   * Inbound client requests route through `src/core/proxy.py`.
   * OpenHuman TokenJuice compression prunes context by 60–80% to minimize LLM inference overhead across OpenAI, Anthropic, and Gemini.
3. **Pre-Bundled Skills Architecture:**
   * Skills reside in `src/skills/` as executable plugins invoked by the OpenHuman agent loop (`src/core/engine.py`).
   * No blank-slate agent setups. Clients get instant value out-of-the-box.
4. **Target Verticals Tested:**
   * **Curtains & Installation:** Voice-note site measurement bookings, instant quote & deposit invoicing, supplier price scraping.
   * **Freelance Photographer:** Portfolio Q&A, Cal.com shoot scheduling, persistent style preferences, invoice delivery.
   * **Home Cleaning Services:** Recurring weekly appointments, Stripe deposit collection, contextual surcharge policies.

---

## 3. Registered Repository Layout & Module Mapping
* `PROJECT_STATE.md`: This file. High-level anchor for LLM grounding.
* `README.md`: Public architectural blueprint and development documentation.
* `ROADMAP.md`: Granular phase milestones and progress checklist.
* `config/tenants.yaml`: Active tenant registry, domain targets, and subscription tiers.
* `config/default_settings.yaml`: Global model routing parameters, compression thresholds, and timeouts.
* `docker/Dockerfile.core`: Base lightweight OpenHuman container image.
* `docker/docker-compose.yml`: Multi-tenant orchestrator and central proxy stack.
* `src/core/engine.py`: OpenHuman execution loop and lifecycle manager.
* `src/core/proxy.py`: Unified LLM router and TokenJuice compression engine.
* `src/gateway/whatsapp.py`: Webhook ingress for Twilio / WhatsApp Cloud API.
* `src/skills/calendar_sync.py`: Google Calendar & Cal.com bi-directional booking engine.
* `src/skills/invoicing.py`: Automated PDF invoice generator and Stripe link dispatch.
* `src/skills/research.py`: Headless search and supplier price scraping.
* `src/skills/memory_tree.py`: SQLite-backed context graph (persistent business rules).
* `src/skills/seo_manager.py`: Weekly automated technical SEO audits, keyword monitoring, and WhatsApp digests.
* `src/utils/logger.py`: Centralized structured logging.
* `data/tenants/`: Volume-mounted storage for isolated client SQLite databases.

---

## 4. Active Decisions & Current State
* **Current Phase:** Phase 1 (Architecture, Grounding Documentation & Docker Infrastructure).
* **Completed:** Business model finalized, repository scaffolding initialized and committed to GitHub main branch.
* **Next Immediate Step:** Write `docker/Dockerfile.core` and production `docker/docker-compose.yml`.