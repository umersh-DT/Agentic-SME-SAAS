import hmac
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Dict

import aiosqlite
from fastapi import APIRouter, Header, HTTPException, Request, Response

logger = logging.getLogger("gateway_stripe")

router = APIRouter(prefix="/webhook", tags=["Stripe Billing"])

TENANT_ID_REGEX = re.compile(r"^tenant_[a-z0-9_]+$")


def verify_stripe_signature(payload: bytes, sig_header: str, secret: str, tolerance: int = 300) -> bool:
    """Verifies Stripe webhook HMAC-SHA256 signature and guards against replay attacks."""
    if not sig_header or not secret:
        return False

    elements = sig_header.split(",")
    timestamp = None
    v1_signatures = []

    for element in elements:
        element = element.strip()
        if element.startswith("t="):
            timestamp = element[2:]
        elif element.startswith("v1="):
            v1_signatures.append(element[3:])

    if not timestamp or not v1_signatures:
        return False

    # Check replay window
    try:
        ts_int = int(timestamp)
        if abs(time.time() - ts_int) > tolerance:
            logger.warning("[STRIPE AUTH] Timestamp outside acceptable tolerance.")
            return False
    except ValueError:
        return False

    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()

    return any(hmac.compare_digest(expected, sig) for sig in v1_signatures)


async def provision_tenant_storage(tenant_id: str, plan_tier: str, base_data_dir: str = "/app/data/tenants"):
    """Provisions an isolated SQLite database file for a newly subscribed tenant."""
    # Strict regex check against path traversal
    if not TENANT_ID_REGEX.match(tenant_id):
        raise ValueError(f"Invalid tenant_id format: '{tenant_id}'. Must match ^tenant_[a-z0-9_]+$")

    target_dir = Path(base_data_dir if Path(base_data_dir).exists() else "data/tenants")
    target_dir.mkdir(parents=True, exist_ok=True)
    db_path = target_dir / f"{tenant_id}.sqlite"

    if not db_path.exists():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA journal_mode = WAL;")
            await db.execute("PRAGMA foreign_keys = ON;")
            await db.commit()
        logger.info(f"[PROVISION] Created isolated tenant DB: {db_path.name}")
    else:
        logger.info(f"[PROVISION] Tenant DB already exists: {db_path.name}")


@router.post("/stripe")
async def handle_stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="Stripe-Signature"),
):
    """Processes Stripe recurring billing webhooks."""
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

    # Fail-closed security
    if not webhook_secret:
        logger.error("[SECURITY] STRIPE_WEBHOOK_SECRET is unset. Rejecting webhook (fail-closed).")
        raise HTTPException(status_code=400, detail="Webhook signing secret not configured.")

    payload = await request.body()
    if not verify_stripe_signature(payload, stripe_signature, webhook_secret):
        logger.warning("[SECURITY] Invalid Stripe webhook signature.")
        raise HTTPException(status_code=400, detail="Invalid Stripe signature.")

    try:
        event = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    event_type = event.get("type")
    data_object = event.get("data", {}).get("object", {})

    if event_type == "checkout.session.completed":
        metadata = data_object.get("metadata", {})
        tenant_id = metadata.get("tenant_id")
        plan_tier = metadata.get("plan_tier", "pro")

        if not tenant_id:
            logger.error("[STRIPE ERROR] checkout.session.completed missing tenant_id in metadata.")
            raise HTTPException(status_code=400, detail="Missing tenant_id in metadata.")

        if not TENANT_ID_REGEX.match(tenant_id):
            logger.error(f"[SECURITY REJECT] Invalid tenant_id in Stripe metadata: {tenant_id}")
            raise HTTPException(status_code=400, detail="Invalid tenant_id format.")

        logger.info(f"[STRIPE BILLING] Checkout completed for tenant: {tenant_id} (Tier: {plan_tier})")
        await provision_tenant_storage(tenant_id=tenant_id, plan_tier=plan_tier)

    elif event_type in ("customer.subscription.deleted", "customer.subscription.paused"):
        logger.info(f"[STRIPE BILLING] Subscription event {event_type} received.")

    return Response(status_code=200, content='{"status":"success"}', media_type="application/json")