from fastapi import FastAPI

app = FastAPI(title="Agentic SME SaaS Gateway", version="0.1.0")

@app.get("/health")
def health_check():
    return {"status": "ok", "phase": "1_infrastructure_verified"}