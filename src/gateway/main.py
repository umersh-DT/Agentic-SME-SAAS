import time
from fastapi import FastAPI, Request, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

from src.gateway.twilio_webhook import router as twilio_router
from src.gateway.stripe_billing import router as stripe_router

app = FastAPI(title="Agentic SaaS Ingress Gateway", version="1.0.0")

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
app.include_router(twilio_router)
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