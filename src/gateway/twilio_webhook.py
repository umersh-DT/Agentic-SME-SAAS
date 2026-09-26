import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Dict, Optional, Set
from urllib.parse import parse_qsl

from fastapi import APIRouter, Header, HTTPException, Request, Response
import yaml

from src.utils.security import TenantConfig, normalize_phone_number

logger = logging.getLogger("gateway_twilio")

router = APIRouter(prefix="/webhook", tags=["Twilio Ingress"])


class DeduplicationCache:
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


class RejectionRateLimiter:
    """Limits rejection TwiML to once per 24 hours (86,400 seconds) per sender to suppress spam."""

    def __init__(self, cooldown_seconds: int = 86400):
        self.cooldown = cooldown_seconds
        self._last_reply: Dict[str, float] = {}

    def should_reply(self, phone: str) -> bool:
        now = time.time()
        self._purge_stale(now)
        last = self._last_reply.get(phone, 0.0)
        if now - last < self.cooldown:
            return False
        self._last_reply[phone] = now
        return True

    def _purge_stale(self, now: float) -> None:
        stale_threshold = now - (self.cooldown * 2)
        expired = [p for p, ts in self._last_reply.items() if ts < stale_threshold]
        for p in expired:
            del self._last_reply[p]


dedup_cache = DeduplicationCache()
rejection_limiter = RejectionRateLimiter(cooldown_seconds=86400)


class StrictTenantDirectory:
    def __init__(self, config_path: str = "config/tenants.yaml"):
        self.config_path = config_path
        self.phone_to_tenant: Dict[str, str] = {}
        self.tenants: Dict[str, TenantConfig] = {}
        self.collided_phones: Set[str] = set()
        self.reload_tenants()

    def reload_tenants(self) -> None:
        effective_path = os.environ.get("TENANTS_CONFIG_PATH", self.config_path)
        if not os.path.exists(effective_path):
            logger.warning(f"Tenant configuration file not found at: {effective_path}")
            return

        with open(effective_path, "r", encoding="utf-8") as f:
            raw_data = yaml.safe_load(f) or {}

        tenants_raw = raw_data.get("tenants", [])
        tenant_list = tenants_raw.values() if isinstance(tenants_raw, dict) else tenants_raw

        self.phone_to_tenant.clear()
        self.tenants.clear()
        self.collided_phones.clear()

        for item in tenant_list:
            if "id" in item and "tenant_id" not in item:
                item["tenant_id"] = item["id"]
            if "active_plan" in item and "plan_tier" not in item:
                item["plan_tier"] = item["active_plan"]

            try:
                config = TenantConfig(**item)
            except Exception as e:
                logger.critical(f"[STARTUP FATAL] Invalid tenant schema in {effective_path}: {e}")
                raise ValueError(f"Startup failed: invalid tenant configuration: {e}") from e

            self.tenants[config.tenant_id] = config

            for phone in config.get_all_associated_phones():
                if phone in self.collided_phones:
                    continue

                if phone in self.phone_to_tenant and self.phone_to_tenant[phone] != config.tenant_id:
                    other_tenant = self.phone_to_tenant.pop(phone)
                    self.collided_phones.add(phone)
                    logger.error(
                        f"[FATAL PHONE COLLISION] Phone {phone} claimed by both {other_tenant} and "
                        f"{config.tenant_id}. Dropped from both."
                    )
                else:
                    self.phone_to_tenant[phone] = config.tenant_id

        logger.info(
            f"Loaded {len(self.tenants)} tenants with {len(self.phone_to_tenant)} unique active phone mappings."
        )

    def resolve_sender(self, from_number: str) -> Optional[str]:
        norm = normalize_phone_number(from_number)
        return self.phone_to_tenant.get(norm)


tenant_directory = StrictTenantDirectory()


def verify_twilio_signature(url: str, post_data: bytes, signature: str, auth_token: str) -> bool:
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
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()

    if not auth_token:
        logger.error("[SECURITY] TWILIO_AUTH_TOKEN is unset. Rejecting webhook (fail-closed).")
        raise HTTPException(status_code=403, detail="Webhook authentication is unconfigured.")

    raw_body = await request.body()
    effective_url = str(request.url)

    if not x_twilio_signature or not verify_twilio_signature(effective_url, raw_body, x_twilio_signature, auth_token):
        logger.warning(f"[SECURITY] Invalid Twilio signature")
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    form_params = dict(parse_qsl(raw_body.decode("utf-8", errors="ignore"), keep_blank_values=True))
    from_number = form_params.get("From", "")
    message_sid = form_params.get("MessageSid", "")

    if not message_sid:
        return Response(content="<Response></Response>", media_type="application/xml")

    if dedup_cache.is_duplicate(message_sid):
        logger.warning(f"[DEDUP] Dropping duplicate Twilio message MessageSid={message_sid}")
        return Response(content="<Response></Response>", media_type="application/xml", headers={"X-Dedup-Dropped": "true"})

    tenant_id = tenant_directory.resolve_sender(from_number)
    if not tenant_id:
        logger.warning(f"[ROUTING REJECT] Unregistered sender: {from_number}.")
        if rejection_limiter.should_reply(from_number):
            rejection_twiml = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<Response>\n"
                "  <Message>This phone number is not registered with an active business assistant. "
                "Please contact your business administrator.</Message>\n"
                "</Response>"
            )
            return Response(content=rejection_twiml, media_type="application/xml")
        return Response(content="<Response></Response>", media_type="application/xml")

    logger.info(f"[INGRESS ACCEPTED] MessageSid={message_sid} | Tenant={tenant_id} | Sender={from_number}")
    return Response(content="<Response></Response>", media_type="application/xml")