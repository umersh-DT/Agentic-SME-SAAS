import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Dict, Optional, Set
from urllib.parse import parse_qsl

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, Response
import yaml

from src.core.storage_models import TenantDatabaseManager, load_default_settings
from src.gateway.dispatcher import TenantWorkerDispatcher
from src.skills.whatsapp_reply import WhatsAppReplySkill
from src.utils.security import TenantConfig, describe_error_safely, mask_phone_number, normalize_phone_number

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
dispatcher = TenantWorkerDispatcher()
reply_skill = WhatsAppReplySkill()


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
                detail = describe_error_safely(e)
                logger.critical(f"[STARTUP FATAL] Invalid tenant schema in {effective_path}: {detail}")
                raise ValueError(f"Startup failed: invalid tenant configuration: {detail}") from None

            self.tenants[config.tenant_id] = config

            for phone in config.get_all_associated_phones():
                if phone in self.collided_phones:
                    continue

                if phone in self.phone_to_tenant and self.phone_to_tenant[phone] != config.tenant_id:
                    other_tenant = self.phone_to_tenant.pop(phone)
                    self.collided_phones.add(phone)
                    logger.error(
                        f"[FATAL PHONE COLLISION] Phone {mask_phone_number(phone)} claimed by both {other_tenant} and "
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


async def replay_all_pending_messages(base_data_dir: Optional[str] = None) -> None:
    """Startup routine to scan and replay pending messages across all registered tenants."""
    logger.info("[STARTUP REPLAY] Scanning for unfinished pending messages across tenants...")
    for tenant_id, config in tenant_directory.tenants.items():
        try:
            replayed = await dispatcher.replay_pending_messages(
                tenant_id=tenant_id,
                plan_tier=config.plan_tier,
                business_name=config.business_name,
                owner_phone=config.owner_phone,
            )
            if replayed:
                logger.info(f"[STARTUP REPLAY] Replayed {len(replayed)} messages for tenant={tenant_id}")
        except Exception as e:
            logger.error(f"[STARTUP REPLAY ERROR] Tenant={tenant_id} replay failed: {e}")


@router.post("/whatsapp")
async def handle_whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_twilio_signature: Optional[str] = Header(None, alias="X-Twilio-Signature"),
):
    """Twilio WhatsApp Inbound Webhook handler."""
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()

    if not auth_token:
        logger.error("[SECURITY] TWILIO_AUTH_TOKEN is unset. Rejecting webhook (fail-closed).")
        raise HTTPException(status_code=403, detail="Webhook authentication is unconfigured.")

    raw_body = await request.body()
    effective_url = str(request.url)

    if not x_twilio_signature or not verify_twilio_signature(effective_url, raw_body, x_twilio_signature, auth_token):
        logger.warning("[SECURITY] Invalid Twilio signature")
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    form_params = dict(parse_qsl(raw_body.decode("utf-8", errors="ignore"), keep_blank_values=True))
    from_number = form_params.get("From", "")
    body_text = form_params.get("Body", "").strip()
    message_sid = form_params.get("MessageSid", "")
    num_media = int(form_params.get("NumMedia", "0"))

    if not message_sid:
        return Response(content="<Response></Response>", media_type="application/xml")

    # 1. In-Memory Deduplication Check
    if dedup_cache.is_duplicate(message_sid):
        logger.warning(f"[DEDUP] Dropping duplicate Twilio message MessageSid={message_sid}")
        return Response(content="<Response></Response>", media_type="application/xml", headers={"X-Dedup-Dropped": "true"})

    # 2. Strict Sender Resolution & Masked Logging
    tenant_id = tenant_directory.resolve_sender(from_number)
    if not tenant_id:
        masked_sender = mask_phone_number(from_number)
        logger.warning(f"[ROUTING REJECT] Unregistered sender: {masked_sender}.")
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

    tenant_config = tenant_directory.tenants[tenant_id]

    # 3. Crash Replay Safety: Persist inbound message BEFORE acknowledging Twilio
    base_data_dir = os.environ.get("TENANTS_DATA_DIR", "/app/data/tenants")
    db_manager = TenantDatabaseManager(tenant_id=tenant_id, base_dir=base_data_dir)
    await db_manager.persist_inbound_message(
        message_sid=message_sid,
        from_number=from_number,
        body=body_text,
        num_media=num_media,
    )

    # 4. Media-Only Message Intercept: Mark completed and reply with advisory
    if num_media > 0 and not body_text:
        settings = load_default_settings()
        media_msg = settings.get("whatsapp", {}).get(
            "media_fallback_reply",
            "Voice notes and media messages are coming soon! Please text your request for now.",
        )
        await db_manager.update_message_status(message_sid, "completed")
        background_tasks.add_task(reply_skill.send_reply, to_number=from_number, message=media_msg)
        return Response(content="<Response></Response>", media_type="application/xml")

    # 5. Hand off to Agent Loop via BackgroundTasks (DB check inside dispatcher drops stale retries)
    background_tasks.add_task(
        dispatcher.process_incoming_message,
        tenant_id=tenant_id,
        plan_tier=tenant_config.plan_tier,
        business_name=tenant_config.business_name,
        owner_phone=tenant_config.owner_phone,
        from_number=from_number,
        body=body_text,
        message_sid=message_sid,
    )

    return Response(content="<Response></Response>", media_type="application/xml")