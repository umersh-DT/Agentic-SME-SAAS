import logging
from fastapi import FastAPI
from src.gateway.twilio_webhook import router as twilio_router
from src.gateway.stripe_billing import router as stripe_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(
    title="Agentic SME SaaS Gateway",
    description="Multi-tenant API Gateway, Twilio WhatsApp & Stripe Billing",
    version="1.0.0",
)

# Register routes
app.include_router(twilio_router)
app.include_router(stripe_router)


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "agentic-gateway",
        "phase": 3,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.gateway.main:app", host="0.0.0.0", port=8000, reload=True)