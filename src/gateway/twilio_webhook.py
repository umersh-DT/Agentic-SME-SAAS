import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Dict, List, Optional
from urllib.parse import parse_qsl

from fastapi import APIRouter, Header, HTTPException, Request, Response
import yaml

logger = logging.getLogger("gateway_twilio")

router = APIRouter(prefix="/webhook", tags=["Twilio Ingress"])


class DeduplicationCache:
    """In-memory sliding-window cache to drop duplicate Twilio MessageSids."""

    def __init__(self, ttl_seconds: int = 600):
        self.ttl = ttl_seconds
        self._cache: Dict[str, float] = {}

    def is_duplicate(self, message_sid: str) -> bool:
        now = time.time()
        self._purge_expired(now)
        if message_sid in self._cache:
            return True
        self._cache[message_sid] = now
        return False

    def _purge_expired(self, current_time: float) -> None:
        expired = [sid for sid, timestamp in self._cache.items() if current_time - timestamp > self.ttl]
        for sid in expired:
            del self._cache[sid]


dedup_cache = DeduplicationCache()


class TenantDirectory:
    """Loads tenant registry and resolves incoming sender phone numbers to tenant IDs."""

    def __init__(self, config_path: str = "config/tenants.yaml"):
        self.config_path = config_path
        self.phone_to_tenant: Dict[str, str] = {}
        self.reload_tenants()

    @staticmethod
    def normalize_phone(phone: Optional[str]) -> str:
        """Strips whatsapp: prefixes, spaces, and hyphens to normalize to standard E.164."""
        if not phone:
            return ""
        cleaned = phone.strip()
        if cleaned.startswith("whatsapp:"):
            cleaned = cleaned.replace("whatsapp:", "")
        return cleaned.replace(" ", "").replace("-", "").strip()

    def reload_tenants(self) -> None:
        if not os.path.exists(self.config_path):
            logger.warning(f"Tenant configuration file not found at: {self.config_path}")
            return

        with open(self.config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        tenants = data.get("tenants", [])
        self.phone_to_tenant.clear()

        # Support both list of dicts (schema 1) and dict of dicts (schema 2)
        tenant_iterable = tenants.values() if isinstance(tenants, dict) else tenants

        for t in tenant_iterable:
            tenant_id = t.get("id") or t.get("tenant_id")
            if not tenant_id:
                continue

            candidate_phones: List[str] = []
            if t.get("owner_phone"):
                candidate_phones.append(t.get("owner_phone"))
            if t.get("contact_phone"):
                candidate_phones.append(t.get("contact_phone"))
            if t.get("whatsapp_number"):
                candidate_phones.append(t.get("whatsapp_number"))

            staff_phones = t.get("staff_phones", [])
            if isinstance(staff_phones, list):
                candidate_phones.extend(staff_phones)

            for raw_phone in candidate_phones:
                norm = self.normalize_phone(raw_phone)
                if norm:
                    if norm in self.phone_to_tenant and self.phone_to_tenant[norm] != tenant_id:
                        logger.error(
                            f"Phone collision: {norm} is mapped to multiple tenants "
                            f"({self.phone_to_tenant[norm]} and {tenant_id}). Strict 1-to-1 required."
                        )
                    self.phone_to_tenant[norm] = tenant_id

        logger.info(f"Loaded {len(self.phone_to_tenant)} phone mappings across tenants.")

    def resolve_sender(self, from_number: str) -> Optional[str]:
        norm = self.normalize_phone(from_number)
        return self.phone_to_tenant.get(norm)


tenant_directory = TenantDirectory()


def verify_twilio_signature(url: str, post_data: bytes, signature: str, auth_token: str) -> bool:
    """Validates the X-Twilio-Signature HMAC-SHA1 header against post params."""
    if not auth_token:
        return False
    data_dict = dict(parse_qsl(post_data.decode("utf-8", errors="ignore"), keep_blank_values=True))
    concatenated = url + "".join(f"{k}{v}" for k, v in sorted(data_dict.items()))
    computed = base64.b64encode(
        hmac.new(auth_token.encode("utf-8"), concatenated.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")
    return hmac.compare_digest(computed, signature)


@router.post("/whatsapp")
async def handle_whatsapp_webhook(
    request: Request,
    x_twilio_signature: Optional[str] = Header(None, alias="X-Twilio-Signature"),
):
    """Twilio WhatsApp Inbound Webhook handler."""
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()

    # Fail-closed security: reject if auth token is missing
    if not auth_token:
        logger.error("[SECURITY] TWILIO_AUTH_TOKEN is unset. Rejecting webhook (fail-closed).")
        raise HTTPException(status_code=403, detail="Webhook authentication is unconfigured.")

    # Read raw body directly without Form(...) consuming the stream
    raw_body = await request.body()
    effective_url = str(request.url)

    if not x_twilio_signature or not verify_twilio_signature(effective_url, raw_body, x_twilio_signature, auth_token):
        logger.warning(f"[SECURITY] Invalid Twilio signature from {request.client.host if request.client else 'unknown'}")
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    # Parse form parameters with empty value preservation
    form_params = dict(parse_qsl(raw_body.decode("utf-8", errors="ignore"), keep_blank_values=True))
    from_number = form_params.get("From", "")
    message_sid = form_params.get("MessageSid", "")

    if not message_sid:
        logger.warning("[GATEWAY] Missing MessageSid in valid webhook payload.")
        return Response(content="<Response></Response>", media_type="application/xml")

    # Deduplication check
    if dedup_cache.is_duplicate(message_sid):
        logger.warning(f"[DEDUP] Dropping duplicate Twilio message MessageSid={message_sid}")
        return Response(content="<Response></Response>", media_type="application/xml")

    # Resolve tenant strictly by sender (From)
    tenant_id = tenant_directory.resolve_sender(from_number)
    if not tenant_id:
        logger.warning(f"[ROUTING REJECT] Unregistered sender: {from_number}. Discarding message without tenant data access.")
        return Response(content="<Response></Response>", media_type="application/xml")

    logger.info(f"[INGRESS ACCEPTED] MessageSid={message_sid} | Tenant={tenant_id} | Sender={from_number}")
    return Response(content="<Response></Response>", media_type="application/xml")