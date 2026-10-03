import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, Response
import yaml

from src.core.storage_models import TenantDatabaseManager, load_default_settings
from src.gateway.dispatcher import TenantWorkerDispatcher
from src.skills.whatsapp_reply import WhatsAppReplySkill
from src.utils.security import TenantConfig, describe_error_safely, mask_phone_number, normalize_phone_number

logger = logging.getLogger("gateway_whatsapp")

router = APIRouter(prefix="/webhook", tags=["WhatsApp Cloud API Ingress"])


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


NOT_REGISTERED_NOTICE = (
    "This phone number is not registered with an active business assistant. "
    "Please contact your business administrator."
)

# Voice notes are transcribed; other media types get a "not supported yet" reply.
VOICE_MESSAGE_TYPES = {"audio", "voice"}
MEDIA_MESSAGE_TYPES = {"audio", "voice", "image", "video", "document", "sticker"}


def verify_meta_signature(raw_body: bytes, signature_header: Optional[str], app_secret: str) -> bool:
    """Checks X-Hub-Signature-256 ("sha256=<hex>") = HMAC-SHA256 of the raw body with the app secret."""
    if not app_secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header[len("sha256="):].strip())


def to_e164(wa_id: str) -> str:
    """Meta sends sender numbers as digits without '+' (e.g. 971501234567); routing uses E.164."""
    digits = normalize_phone_number(wa_id).lstrip("+")
    return f"+{digits}" if digits else ""


def extract_inbound_messages(payload: Dict[str, Any], phone_number_id: str) -> List[Dict[str, Any]]:
    """Returns the user messages in a webhook payload; status updates and other numbers are ignored."""
    messages: List[Dict[str, Any]] = []
    if payload.get("object") != "whatsapp_business_account":
        return messages
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            metadata_number_id = str((value.get("metadata") or {}).get("phone_number_id", ""))
            if phone_number_id and metadata_number_id and metadata_number_id != phone_number_id:
                logger.warning("[WEBHOOK] Ignoring payload for a different WhatsApp phone number id.")
                continue
            messages.extend(value.get("messages") or [])
    return messages


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


@router.get("/whatsapp")
async def verify_whatsapp_webhook(request: Request):
    """Meta webhook verification: echo hub.challenge when hub.verify_token matches."""
    verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")

    if not verify_token:
        logger.error("[SECURITY] WHATSAPP_VERIFY_TOKEN is unset. Rejecting verification (fail-closed).")
        raise HTTPException(status_code=403, detail="Webhook verification is unconfigured.")
    if mode == "subscribe" and hmac.compare_digest(token, verify_token):
        logger.info("[WEBHOOK] Meta webhook verified.")
        return Response(content=challenge, media_type="text/plain")
    logger.warning("[SECURITY] Meta webhook verification failed (bad mode or token).")
    raise HTTPException(status_code=403, detail="Verification failed.")


async def _handle_inbound_message(message: Dict[str, Any], background_tasks: BackgroundTasks) -> str:
    """Routes one inbound message. Returns what happened (for logs and tests)."""
    message_id = str(message.get("id", ""))
    from_number = to_e164(str(message.get("from", "")))
    msg_type = message.get("type", "")
    if not message_id or not from_number:
        return "ignored_malformed"

    # 1. In-memory deduplication (Meta retries deliveries)
    if dedup_cache.is_duplicate(message_id):
        logger.warning(f"[DEDUP] Dropping duplicate WhatsApp message id={message_id}")
        return "duplicate"

    # 2. Strict sender resolution
    tenant_id = tenant_directory.resolve_sender(from_number)
    if not tenant_id:
        logger.warning(f"[ROUTING REJECT] Unregistered sender: {mask_phone_number(from_number)}.")
        if rejection_limiter.should_reply(from_number):
            background_tasks.add_task(reply_skill.send_reply, to_number=from_number, message=NOT_REGISTERED_NOTICE)
            return "unregistered_notified"
        return "unregistered_silent"

    tenant_config = tenant_directory.tenants[tenant_id]
    body_text = ((message.get("text") or {}).get("body") or "").strip() if msg_type == "text" else ""
    is_media = msg_type in MEDIA_MESSAGE_TYPES

    if not body_text and not is_media:
        # Reactions, location pins, button taps etc. are not handled yet.
        logger.info(f"[WEBHOOK] Ignoring unsupported message type '{msg_type}' for tenant={tenant_id}.")
        return "ignored_type"

    media_id = str((message.get(msg_type) or {}).get("id", "")) if is_media else ""

    # 3. Crash replay safety: persist before acknowledging Meta
    base_data_dir = os.environ.get("TENANTS_DATA_DIR", "/app/data/tenants")
    db_manager = TenantDatabaseManager(tenant_id=tenant_id, base_dir=base_data_dir)
    await db_manager.persist_inbound_message(
        message_sid=message_id,
        from_number=from_number,
        body=body_text,
        num_media=1 if is_media else 0,
        media_id=media_id or None,
    )

    # 4. Voice notes: download + transcribe after acknowledging, then handled like text
    if msg_type in VOICE_MESSAGE_TYPES and media_id:
        background_tasks.add_task(
            dispatcher.process_voice_message,
            tenant_id=tenant_id,
            from_number=from_number,
            media_id=media_id,
            message_sid=message_id,
            plan_tier=tenant_config.plan_tier,
            business_name=tenant_config.business_name,
            owner_phone=tenant_config.owner_phone,
        )
        return "voice_dispatched"

    # 5. Other media: not supported yet
    if is_media:
        settings = load_default_settings()
        media_msg = settings.get("whatsapp", {}).get(
            "media_fallback_reply",
            "Voice notes and media messages are coming soon! Please text your request for now.",
        )
        await db_manager.update_message_status(message_id, "completed")
        background_tasks.add_task(reply_skill.send_reply, to_number=from_number, message=media_msg)
        return "media_advisory"

    # 6. Hand off to the agent loop after acknowledging (dispatcher drops stale retries)
    background_tasks.add_task(
        dispatcher.process_incoming_message,
        tenant_id=tenant_id,
        plan_tier=tenant_config.plan_tier,
        business_name=tenant_config.business_name,
        owner_phone=tenant_config.owner_phone,
        from_number=from_number,
        body=body_text,
        message_sid=message_id,
    )
    return "dispatched"


@router.post("/whatsapp")
async def handle_whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
):
    """WhatsApp Cloud API inbound webhook handler."""
    app_secret = os.getenv("WHATSAPP_APP_SECRET", "").strip()
    if not app_secret:
        logger.error("[SECURITY] WHATSAPP_APP_SECRET is unset. Rejecting webhook (fail-closed).")
        raise HTTPException(status_code=403, detail="Webhook authentication is unconfigured.")

    raw_body = await request.body()
    if not verify_meta_signature(raw_body, x_hub_signature_256, app_secret):
        logger.warning("[SECURITY] Invalid X-Hub-Signature-256")
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.error("[WEBHOOK] Signed payload is not valid JSON; acknowledging to stop retries.")
        return {"status": "ignored_bad_json"}

    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    outcomes = []
    for message in extract_inbound_messages(payload, phone_number_id):
        outcomes.append(await _handle_inbound_message(message, background_tasks))

    return {"status": "ok", "messages": outcomes}
