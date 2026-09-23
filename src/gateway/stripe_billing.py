import hmac
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional
from fastapi import APIRouter, Header, HTTPException, Request, Response, status
import yaml

logger = logging.getLogger("stripe_billing")
router = APIRouter(prefix="/webhook/stripe", tags=["Stripe Billing"])


def verify_stripe_signature(payload: bytes, sig_header: str, secret: str, tolerance: int = 300) -> bool:
    """Verifies Stripe webhook HMAC-SHA256 signature using timestamped tolerance window."""
    if not sig_header or not secret:
        return False

    elements = dict(item.strip().split("=", 1) for item in sig_header.split(",") if "=" in item)
    timestamp = elements.get("t")
    signature = elements.get("v1")

    if not timestamp or not signature:
        return False

    # Prevent replay attacks
    current_time = int(time.time())
    if abs(current_time - int(timestamp)) > tolerance:
        logger.warning("Stripe webhook timestamp outside acceptable tolerance.")
        return False

    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    computed_sig = hmac.new(
        secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed_sig, signature)


def provision_tenant_subscription(
    tenant_id: str,
    tier: str,
    customer_id: str,
    subscription_id: str,
    config_path: str = "/app/config/tenants.yaml",
    data_dir: str = "/app/data/tenants",
) -> Dict[str, Any]:
    """Updates config/tenants.yaml and creates initial SQLite storage directory."""
    path = Path(config_path)
    if not path.exists():
        path = Path("config/tenants.yaml")

    data = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    tenants = data.get("tenants", [])
    tenant_found = False

    for t in tenants:
        if t.get("tenant_id") == tenant_id:
            t["plan_tier"] = tier
            t["stripe_customer_id"] = customer_id
            t["stripe_subscription_id"] = subscription_id
            t["subscription_status"] = "active"
            tenant_found = True
            break

    if not tenant_found:
        tenants.append({
            "tenant_id": tenant_id,
            "business_name": f"Business {tenant_id}",
            "plan_tier": tier,
            "stripe_customer_id": customer_id,
            "stripe_subscription_id": subscription_id,
            "subscription_status": "active",
        })

    data["tenants"] = tenants

    # Write updated tenants configuration
    if path.exists():
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False)

    # Initialize isolated tenant storage folder
    target_data_dir = Path(data_dir)
    if not target_data_dir.exists():
        target_data_dir = Path("data/tenants")
    target_data_dir.mkdir(parents=True, exist_ok=True)

    db_path = target_data_dir / f"{tenant_id}.sqlite"
    if not db_path.exists():
        db_path.touch()

    logger.info(f"[PROVISIONED] Tenant '{tenant_id}' upgraded to tier '{tier}' with SQLite db at {db_path}")
    return {"tenant_id": tenant_id, "tier": tier, "status": "active", "db_path": str(db_path)}


@router.post("")
async def handle_stripe_webhook(
    request: Request,
    stripe_signature: Optional[str] = Header(None, alias="Stripe-Signature"),
):
    """Processes incoming Stripe subscription events (checkout.session.completed, customer.subscription.deleted)."""
    payload = await request.body()
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

    # Enforce signature verification when secret is configured
    if webhook_secret:
        if not stripe_signature or not verify_stripe_signature(payload, stripe_signature, webhook_secret):
            logger.warning("Invalid Stripe webhook signature.")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Stripe signature",
            )

    try:
        event = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload")

    event_type = event.get("type", "")
    event_data = event.get("data", {}).get("object", {})

    if event_type == "checkout.session.completed":
        # Extract metadata passed during checkout session creation
        metadata = event_data.get("metadata", {})
        tenant_id = metadata.get("tenant_id") or "tenant_curtains_001"
        tier = metadata.get("plan_tier") or "pro"
        customer_id = event_data.get("customer", "")
        subscription_id = event_data.get("subscription", "")

        provision_tenant_subscription(
            tenant_id=tenant_id,
            tier=tier,
            customer_id=customer_id,
            subscription_id=subscription_id,
        )

    elif event_type in ("customer.subscription.deleted", "customer.subscription.paused"):
        customer_id = event_data.get("customer", "")
        logger.info(f"[SUBSCRIPTION SUSPENDED] Customer {customer_id} canceled subscription.")

    return {"status": "success", "event_type": event_type}


if __name__ == "__main__":
    # Standalone unit test for Stripe signature verification
    secret = "whsec_test_secret_abc123"
    t = int(time.time())
    dummy_payload = json.dumps({"type": "checkout.session.completed"}).encode("utf-8")
    signed_content = f"{t}.".encode("utf-8") + dummy_payload
    v1_sig = hmac.new(secret.encode("utf-8"), signed_content, hashlib.sha256).hexdigest()
    valid_header = f"t={t},v1={v1_sig}"

    assert verify_stripe_signature(dummy_payload, valid_header, secret) is True
    assert verify_stripe_signature(dummy_payload, f"t={t},v1=bad_signature", secret) is False
    assert verify_stripe_signature(dummy_payload, f"t={t - 500},v1={v1_sig}", secret) is False  # Outside tolerance
    print("[OK] Stripe HMAC-SHA256 signature verification validated successfully.")