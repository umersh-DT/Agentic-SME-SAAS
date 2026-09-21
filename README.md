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
* Docker & Docker Compose v2.0+
* Python 3.11+
* Twilio / WhatsApp Cloud API credentials
* LLM API Key (Gemini, Anthropic, or OpenAI)

### Local Development Setup
1. Clone the repository:
   ```bash
   git clone https://github.com/umersh-DT/Agentic-SME-SAAS.git
   cd Agentic-SME-SAAS
   ```
2. Configure environment variables:
   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```
3. Build and spin up the Docker multi-tenant stack:
   ```bash
   docker compose -f docker/docker-compose.yml up --build
   ```