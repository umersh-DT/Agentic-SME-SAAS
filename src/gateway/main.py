import asyncio
from contextlib import asynccontextmanager
import logging
import time
from fastapi import FastAPI, Request, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

from src.gateway.whatsapp_webhook import (
    router as whatsapp_router,
    replay_all_pending_messages,
)
from src.gateway.stripe_billing import router as stripe_router
from src.core.agent_loop import missing_model_api_key, resolve_model_name
from src.core.storage_models import load_default_settings

logger = logging.getLogger("gateway_main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handles startup recovery tasks and graceful teardown."""
    logger.info("[STARTUP] Initializing Agentic SaaS Gateway lifespan...")
    model_name = resolve_model_name(load_default_settings())
    missing_key = missing_model_api_key(model_name)
    if missing_key:
        # Refuse to start rather than answer every message with an error.
        message = f"AI model '{model_name}' needs {missing_key}, which is not set. Add it to .env."
        logger.critical(f"[STARTUP FATAL] {message}")
        raise RuntimeError(message)
    logger.info(f"[STARTUP] AI model: {model_name}")
    try:
        # Replay any pending messages left unfinished across tenants after a restart
        await replay_all_pending_messages()
    except Exception as e:
        logger.error(f"[STARTUP ERROR] Error replaying pending messages during boot: {e}", exc_info=True)
    yield
    logger.info("[SHUTDOWN] Terminating Agentic SaaS Gateway lifespan...")


app = FastAPI(
    title="Agentic SaaS Ingress Gateway",
    version="1.0.0",
    lifespan=lifespan,
)

# --- Prometheus Metrics Definitions ---
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests received",
    ["method", "endpoint", "status_code"],
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["endpoint"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)


@app.middleware("http")
async def prometheus_metrics_middleware(request: Request, call_next):
    endpoint = request.url.path
    start_time = time.perf_counter()
    try:
        response: Response = await call_next(request)
        status_code = response.status_code
    except Exception:
        status_code = 500
        raise
    finally:
        duration = time.perf_counter() - start_time
        # Record metrics (skip /metrics itself to prevent poll amplification)
        if endpoint != "/metrics":
            HTTP_REQUESTS_TOTAL.labels(
                method=request.method,
                endpoint=endpoint,
                status_code=status_code,
            ).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(endpoint=endpoint).observe(duration)
    return response


# Include Ingress Routers
app.include_router(whatsapp_router)
app.include_router(stripe_router)


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "agentic-gateway",
        "phase": 4,
    }


@app.get("/metrics")
async def metrics():
    """Prometheus scrape endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)