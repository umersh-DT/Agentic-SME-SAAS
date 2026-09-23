import base64
import hashlib
import hmac
import logging
import os
import time
from pathlib import Path
from typing import Dict, Optional, Set
import yaml
from fastapi import APIRouter, Form, Header, HTTPException, Request, Response, status
from pydantic import BaseModel

logger = logging.getLogger("twilio_gateway")
router = APIRouter(prefix="/webhook", tags=["Twilio WhatsApp"])


class DeduplicationCache:
    """Sliding-window in-memory deduplicator for Twilio MessageSid payloads.

    Maintains processed MessageSids with a 10-minute TTL to block retry loops.
    """

    def __init__(self, ttl_seconds: int = 600):
        self.ttl = ttl_seconds
        self._cache: Dict[str, float] = {}

    def is_duplicate(self, message_sid: str) -> bool:
        now = time.time()
        # Clean expired entries
        self._cache = {sid: ts for sid, ts in self._cache.items() if now - ts < self.ttl}
        if message_sid in self._cache:
            return True
        self._cache[message_sid] = now
        return False


dedup_cache = DeduplicationCache()


class TenantDirectory:
    """Loads and matches tenant metadata from config/tenants.yaml by WhatsApp phone number."""

    def __init__(self, config_path: str = "/app/config/tenants.yaml"):
        self.config_path = config_path
        self._phone_to_tenant: Dict[str, str] = {}
        self.reload()

    def reload(self):
        path = Path(self.config_path)
        if not path.exists():
            # Fallback for local testing if running outside /app
            path = Path("config/tenants.yaml")

        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                tenants = data.get("tenants", [])
                for t in tenants:
                    tid = t.get("tenant_id")
                    phone = str(t.get("whatsapp_number", "")).replace("whatsapp:", "").strip()
                    if phone and tid:
                        self._phone_to_tenant[phone] = tid
                        # Also register stripped '+' variant
                        self._phone_to_tenant[phone.lstrip("+")] = tid

    def resolve_tenant(self, to_number: str, from_number: str) -> Optional[str]:
        clean_to = to_number.replace("whatsapp:", "").strip()
        clean_from = from_number.replace("whatsapp:", "").strip()

        # Destination number matches business tenant
        if clean_to in self._phone_to_tenant:
            return self._phone_to_tenant[clean_to]
        if clean_to.lstrip("+") in self._phone_to_tenant:
            return self._phone_to_tenant[clean_to.lstrip("+")]

        # Direct owner communication where owner WhatsApp matches
        if clean_from in self._phone_to_tenant:
            return self._phone_to_tenant[clean_from]

        return None


tenant_dir = TenantDirectory()


def verify_twilio_signature(
    url: str,
    params: Dict[str, str],
    expected_signature: str,
    auth_token: str,
) -> bool:
    """Validates X-Twilio-Signature header using HMAC-SHA1 to prevent spoofing."""
    if not auth_token or not expected_signature:
        return False

    sorted_keys = sorted(params.keys())
    data = url + "".join(f"{key}{params[key]}" for key in sorted_keys)

    computed_mac = hmac.new(
        auth_token.encode("utf-8"),
        data.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    computed_signature = base64.b64encode(computed_mac).decode("utf-8")
    return hmac.compare_digest(computed_signature, expected_signature)


@router.post("/whatsapp")
async def handle_whatsapp_webhook(
    request: Request,
    x_twilio_signature: Optional[str] = Header(None, alias="X-Twilio-Signature"),
    MessageSid: str = Form(...),
    From: str = Form(...),
    To: str = Form(...),
    Body: str = Form(""),
):
    """Processes incoming WhatsApp messages with idempotency checks and tenant routing."""
    # 1. Twilio Signature Verification (Enforced if TWILIO_AUTH_TOKEN is set)
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if auth_token:
        form_data = await request.form()
        params = {k: str(v) for k, v in form_data.items()}
        request_url = str(request.url)
        if not verify_twilio_signature(request_url, params, x_twilio_signature or "", auth_token):
            logger.warning(f"Rejected unauthenticated Twilio webhook payload from {request.client.host}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid Twilio signature header",
            )

    # 2. Idempotency Check
    if dedup_cache.is_duplicate(MessageSid):
        logger.info(f"Duplicate MessageSid {MessageSid} detected. Returning cached ACK.")
        return Response(content="<Response></Response>", media_type="application/xml")

    # 3. Dynamic Tenant Resolution
    tenant_id = tenant_dir.resolve_tenant(to_number=To, from_number=From)
    if not tenant_id:
        # Default fallback to first configured tenant for dev/testing environments
        tenant_id = "tenant_curtains_001"

    logger.info(
        f"[ROUTED] WhatsApp Msg SID={MessageSid} | Tenant={tenant_id} | "
        f"From={From} | Preview='{Body[:30]}...'"
    )

    # Empty TwiML instructs Twilio not to send an immediate synchronous reply
    return Response(content="<Response></Response>", media_type="application/xml")