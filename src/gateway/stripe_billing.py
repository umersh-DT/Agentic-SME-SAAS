import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import stat
import tempfile
import time
from typing import Any, Dict, List, Optional
import yaml

from fastapi import APIRouter, Header, HTTPException, Request, Response

from src.utils.security import TENANT_ID_REGEX, TenantConfig, get_safe_tenant_storage_path, normalize_phone_number

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

    return any(hmac.compare_digest(expected_sig, sig) for sig in v1_signatures)


def _sync_provision_tenant_storage(tenant_id: str, plan_tier: str, base_dir: str = "/app/data/tenants") -> None:
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
    await asyncio.to_thread(_sync_provision_tenant_storage, tenant_id, plan_tier, base_dir)


def atomic_write_yaml(file_path: str, data: dict) -> None:
    """Safely writes YAML data using an atomic replace operation and sets readable file permissions (0644)."""
    dir_name = os.path.dirname(os.path.abspath(file_path))
    os.makedirs(dir_name, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
        yaml.safe_dump(data, tf, sort_keys=False)
        temp_name = tf.name

    # Ensure readable permissions across host and container (rw-r--r--)
    try:
        os.chmod(temp_name, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    except OSError:
        pass

    os.replace(temp_name, file_path)


def register_tenant_in_yaml(
    candidate_config: TenantConfig,
    config_path: str = "config/tenants.yaml",
) -> None:
    """Atomically updates config/tenants.yaml.
    
    If tenant_id exists: updates ONLY billing metadata, preserving all business details and phone routing.
    If tenant_id is new: appends the validated TenantConfig record.
    """
    config_file = os.environ.get("TENANTS_CONFIG_PATH", config_path)
    data = {"tenants": []}

    if os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {"tenants": []}

    tenants = data.get("tenants", [])
    found = False

    for idx, t in enumerate(tenants):
        curr_id = t.get("tenant_id") or t.get("id")
        if curr_id == candidate_config.tenant_id:
            # Selective update of billing-only fields
            t["plan_tier"] = candidate_config.plan_tier
            t["subscription_status"] = "active"
            if candidate_config.stripe_customer_id:
                t["stripe_customer_id"] = candidate_config.stripe_customer_id
            if candidate_config.stripe_subscription_id:
                t["stripe_subscription_id"] = candidate_config.stripe_subscription_id
            found = True
            break

    if not found:
        tenants.append(candidate_config.model_dump())

    data["tenants"] = tenants
    atomic_write_yaml(config_file, data)

    from src.gateway.twilio_webhook import tenant_directory
    tenant_directory.reload_tenants()


@router.post("/stripe")
async def handle_stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="Stripe-Signature"),
):
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

    if not secret:
        logger.error("[SECURITY] STRIPE_WEBHOOK_SECRET is unset. Rejecting (fail-closed).")
        raise HTTPException(status_code=400, detail="Webhook secret is unconfigured.")

    payload = await request.body()

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
        raw_owner_phone = metadata.get("owner_phone")
        customer_id = session_obj.get("customer")
        subscription_id = session_obj.get("subscription")

        if not tenant_id:
            logger.error("[STRIPE BAD DATA] Missing tenant_id in metadata. Returning 200 to halt retries.")
            return Response(content='{"status":"ignored_missing_tenant_id"}', media_type="application/json")

        if not TENANT_ID_REGEX.match(tenant_id):
            logger.error(f"[SECURITY REJECT] Malformed tenant_id in Stripe metadata: {tenant_id}. Dropping.")
            return Response(content='{"status":"ignored_malformed_tenant_id"}', media_type="application/json")

        if not raw_owner_phone:
            logger.warning(f"[STRIPE METADATA] Missing owner_phone for {tenant_id}. Cannot register tenant routing.")
            return Response(content='{"status":"ignored_missing_owner_phone"}', media_type="application/json")

        if plan_tier not in VALID_PLAN_TIERS:
            logger.warning(f"[STRIPE METADATA] Invalid plan_tier '{plan_tier}', defaulting to starter.")
            plan_tier = "starter"

        # Validate in memory before touching storage or registry
        try:
            candidate_config = TenantConfig(
                tenant_id=tenant_id,
                business_name=business_name,
                plan_tier=plan_tier,
                subscription_status="active",
                owner_phone=raw_owner_phone,
                whatsapp_number=raw_owner_phone,
                staff_phones=[],
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
                enabled_skills=["calendar_sync", "invoicing", "research", "memory_tree"],
            )
        except Exception as e:
            logger.error(f"[STRIPE CUSTOMER DEFECT] Invalid tenant configuration for {tenant_id}: {e}.")
            return Response(content=json.dumps({"status": "ignored_invalid_customer_data", "detail": str(e)}), media_type="application/json")

        from src.gateway.twilio_webhook import tenant_directory

        # CONDITION 1: If tenant_id already exists, prevent phone modification / hijacking
        if tenant_id in tenant_directory.tenants:
            existing_tenant = tenant_directory.tenants[tenant_id]
            norm_checkout_phone = candidate_config.owner_phone
            if norm_checkout_phone != existing_tenant.owner_phone and norm_checkout_phone != existing_tenant.whatsapp_number:
                logger.critical(
                    f"[SECURITY ACCOUNT HIJACK] Checkout for existing {tenant_id} provided phone "
                    f"'{norm_checkout_phone}', which does not match registered owner phone '{existing_tenant.owner_phone}'. "
                    f"Rejecting phone change to protect account routing."
                )
                return Response(
                    content='{"status":"ignored_phone_mismatch_for_existing_tenant"}',
                    media_type="application/json",
                )
        else:
            # New tenant signup: Guard against collision with an existing tenant's number
            norm_phone = candidate_config.owner_phone
            existing_owner = tenant_directory.resolve_sender(norm_phone)
            if existing_owner and existing_owner != tenant_id:
                logger.critical(
                    f"[SECURITY HIJACK ATTEMPT] Phone {norm_phone} for new {tenant_id} is already "
                    f"registered to active tenant {existing_owner}. Aborting registration."
                )
                return Response(
                    content='{"status":"ignored_phone_already_registered"}',
                    media_type="application/json",
                )

        # Operational failures return 500 so Stripe retries
        try:
            base_dir = os.environ.get("TENANTS_DATA_DIR", "/app/data/tenants")
            await provision_tenant_storage(tenant_id=tenant_id, plan_tier=plan_tier, base_dir=base_dir)
            register_tenant_in_yaml(candidate_config)
        except Exception as e:
            logger.exception(f"[PROVISION FATAL] Storage or registration failure for {tenant_id}: {e}")
            raise HTTPException(status_code=500, detail="Internal tenant provisioning failure.")

    return Response(content='{"status":"success"}', media_type="application/json")