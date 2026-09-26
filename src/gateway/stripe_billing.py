import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional
import yaml

from fastapi import APIRouter, Header, HTTPException, Request, Response

from src.utils.security import TENANT_ID_REGEX, TenantConfig, get_safe_tenant_storage_path

logger = logging.getLogger("gateway_stripe")

router = APIRouter(prefix="/webhook", tags=["Stripe Billing Ingress"])

VALID_PLAN_TIERS = {"starter", "pro", "enterprise"}


def verify_stripe_signature(payload: bytes, sig_header: str, secret: str, tolerance: int = 300) -> bool:
    """Verifies Stripe signature supporting timestamp tolerance and multi-key v1 rotation."""
    if not sig_header or not secret:
        return False

    elements = [item.strip() for item in sig_header.split(",") if "=" in item]
    timestamp = None
    v1_signatures: List[str] = []

    for item in elements:
        k, v = item.split("=", 1)
        if k == "t":
            timestamp = v
        elif k == "v1":
            v1_signatures.append(v)

    if not timestamp or not v1_signatures:
        return False

    try:
        ts_int = int(timestamp)
        if abs(time.time() - ts_int) > tolerance:
            logger.warning("[SECURITY] Stripe webhook timestamp outside tolerance window.")
            return False
    except ValueError:
        return False

    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    expected_sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()

    # Support multiple v1 signatures during key rollover
    return any(hmac.compare_digest(expected_sig, sig) for sig in v1_signatures)


def _sync_provision_tenant_storage(tenant_id: str, plan_tier: str, base_dir: str = "/app/data/tenants") -> None:
    """Synchronous SQLite provisioning; executed inside threadpool to keep async loop unblocked."""
    db_path = get_safe_tenant_storage_path(tenant_id, base_dir=base_dir)
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


async def provision_tenant_storage(tenant_id: str, plan_tier: str = "starter", base_dir: str = "/app/data/tenants") -> None:
    """Hermetically provisions SQLite storage asynchronously."""
    await asyncio.to_thread(_sync_provision_tenant_storage, tenant_id, plan_tier, base_dir)


def register_tenant_in_yaml(
    tenant_id: str,
    business_name: str,
    plan_tier: str,
    owner_phone: str,
    customer_id: Optional[str] = None,
    subscription_id: Optional[str] = None,
    config_path: str = "config/tenants.yaml",
) -> None:
    """Updates config/tenants.yaml and refreshes in-memory registry so tenant is instantly routable."""
    config_file = os.environ.get("TENANTS_CONFIG_PATH", config_path)
    data = {"tenants": []}

    if os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {"tenants": []}

    tenants = data.get("tenants", [])
    # Update existing or append new
    found = False
    for t in tenants:
        if t.get("tenant_id") == tenant_id or t.get("id") == tenant_id:
            t["plan_tier"] = plan_tier
            t["subscription_status"] = "active"
            if customer_id:
                t["stripe_customer_id"] = customer_id
            if subscription_id:
                t["stripe_subscription_id"] = subscription_id
            found = True
            break

    if not found:
        new_entry = {
            "tenant_id": tenant_id,
            "business_name": business_name,
            "plan_tier": plan_tier,
            "subscription_status": "active",
            "owner_phone": owner_phone,
            "whatsapp_number": owner_phone,
            "staff_phones": [],
            "stripe_customer_id": customer_id,
            "stripe_subscription_id": subscription_id,
            "enabled_skills": ["calendar_sync", "invoicing", "research", "memory_tree"],
        }
        tenants.append(new_entry)

    data["tenants"] = tenants
    with open(config_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)

    # Reload directory
    from src.gateway.twilio_webhook import tenant_directory
    tenant_directory.reload_tenants()


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
        logger.error("[STRIPE BAD DATA] Invalid JSON payload received. Returning 200 to halt retries.")
        return Response(content='{"status":"dropped_bad_json"}', media_type="application/json")

    event_type = event.get("type", "")

    if event_type == "checkout.session.completed":
        session_obj: Dict[str, Any] = event.get("data", {}).get("object", {})
        metadata = session_obj.get("metadata") or {}
        tenant_id = metadata.get("tenant_id")
        plan_tier = metadata.get("plan_tier", "starter")
        business_name = metadata.get("business_name", f"Business {tenant_id}")
        owner_phone = metadata.get("owner_phone", "")
        customer_id = session_obj.get("customer")
        subscription_id = session_obj.get("subscription")

        # Non-retryable metadata errors return 200 to halt retries
        if not tenant_id:
            logger.error("[STRIPE BAD DATA] Missing tenant_id in metadata. Returning 200 to halt retries.")
            return Response(content='{"status":"ignored_missing_tenant_id"}', media_type="application/json")

        if not TENANT_ID_REGEX.match(tenant_id):
            logger.error(f"[SECURITY REJECT] Malformed tenant_id in Stripe metadata: {tenant_id}. Dropping.")
            return Response(content='{"status":"ignored_malformed_tenant_id"}', media_type="application/json")

        if plan_tier not in VALID_PLAN_TIERS:
            logger.warning(f"[STRIPE METADATA] Invalid plan_tier '{plan_tier}', defaulting to starter.")
            plan_tier = "starter"

        # Internal operational failures must return 500 so Stripe retries
        try:
            base_dir = os.environ.get("TENANTS_DATA_DIR", "/app/data/tenants")
            await provision_tenant_storage(tenant_id=tenant_id, plan_tier=plan_tier, base_dir=base_dir)

            if owner_phone:
                register_tenant_in_yaml(
                    tenant_id=tenant_id,
                    business_name=business_name,
                    plan_tier=plan_tier,
                    owner_phone=owner_phone,
                    customer_id=customer_id,
                    subscription_id=subscription_id,
                )
        except Exception as e:
            logger.exception(f"[PROVISION FATAL] Storage or registration failure for {tenant_id}: {e}")
            raise HTTPException(status_code=500, detail="Internal tenant provisioning failure.")

    return Response(content='{"status":"success"}', media_type="application/json")