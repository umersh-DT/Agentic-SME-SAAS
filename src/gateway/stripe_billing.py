import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict

from fastapi import APIRouter, Header, HTTPException, Request, Response

from src.utils.security import TENANT_ID_REGEX, get_safe_tenant_storage_path

logger = logging.getLogger("gateway_stripe")

router = APIRouter(prefix="/webhook", tags=["Stripe Billing Ingress"])


def verify_stripe_signature(payload: bytes, sig_header: str, secret: str, tolerance: int = 300) -> bool:
    """Verifies Stripe v1 HMAC-SHA256 signature and timestamp tolerance."""
    if not sig_header or not secret:
        return False

    elements = dict(item.strip().split("=", 1) for item in sig_header.split(",") if "=" in item)
    timestamp = elements.get("t")
    v1_sig = elements.get("v1")

    if not timestamp or not v1_sig:
        return False

    # Prevent replay attacks outside tolerance window
    try:
        ts_int = int(timestamp)
        if abs(time.time() - ts_int) > tolerance:
            logger.warning("[SECURITY] Stripe webhook timestamp outside tolerance window.")
            return False
    except ValueError:
        return False

    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    expected_sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_sig, v1_sig)


def provision_tenant_storage(tenant_id: str, plan_tier: str = "starter") -> bool:
    """Hermetically provisions SQLite storage for a new tenant under /app/data/tenants."""
    try:
        db_path = get_safe_tenant_storage_path(tenant_id)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tenant_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cursor.execute(
                "INSERT OR REPLACE INTO tenant_metadata (key, value) VALUES (?, ?);",
                ("plan_tier", plan_tier),
            )
            cursor.execute(
                "INSERT OR REPLACE INTO tenant_metadata (key, value) VALUES (?, ?);",
                ("status", "active"),
            )
            conn.commit()

        logger.info(f"[PROVISION] Successfully initialized SQLite storage for {tenant_id}")
        return True
    except Exception as e:
        logger.error(f"[PROVISION ERROR] Failed to initialize storage for {tenant_id}: {e}")
        return False


@router.post("/stripe")
async def handle_stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="Stripe-Signature"),
):
    """Processes Stripe checkout and subscription events."""
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

    # Fail-closed authentication check
    if not secret:
        logger.error("[SECURITY] STRIPE_WEBHOOK_SECRET is unset. Rejecting (fail-closed).")
        raise HTTPException(status_code=400, detail="Webhook secret is unconfigured.")

    payload = await request.body()

    # 400 is strictly reserved for signature verification failures
    if not stripe_signature or not verify_stripe_signature(payload, stripe_signature, secret):
        logger.warning("[SECURITY] Stripe webhook signature verification failed.")
        raise HTTPException(status_code=400, detail="Invalid Stripe signature.")

    try:
        event = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        logger.error("[STRIPE BAD DATA] Invalid JSON payload received. Returning 200 to drop retries.")
        return Response(content='{"status":"dropped_bad_json"}', media_type="application/json")

    event_type = event.get("type", "")

    if event_type == "checkout.session.completed":
        session_obj: Dict[str, Any] = event.get("data", {}).get("object", {})
        metadata = session_obj.get("metadata") or {}
        tenant_id = metadata.get("tenant_id")
        plan_tier = metadata.get("plan_tier", "starter")

        # Non-retryable metadata errors return 200 so Stripe doesn't repeat for 3 days
        if not tenant_id:
            logger.error("[STRIPE BAD DATA] Checkout completed missing tenant_id metadata. Returning 200 to halt retries.")
            return Response(content='{"status":"ignored_missing_tenant_id"}', media_type="application/json")

        if not TENANT_ID_REGEX.match(tenant_id):
            logger.error(f"[SECURITY REJECT] Malformed tenant_id in Stripe metadata: {tenant_id}. Dropping.")
            return Response(content='{"status":"ignored_malformed_tenant_id"}', media_type="application/json")

        provision_tenant_storage(tenant_id=tenant_id, plan_tier=plan_tier)

    return Response(content='{"status":"success"}', media_type="application/json")