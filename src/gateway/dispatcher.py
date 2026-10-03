import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from src.core.agent_loop import LiteLLMAgent
from src.core.context_extractor import ContextExtractor
from src.core.storage_models import TenantDatabaseManager, load_default_settings
from src.skills.memory_tree import TenantMemoryTree
from src.skills.whatsapp_reply import WhatsAppReplySkill
from src.utils.security import mask_phone_number, normalize_phone_number

logger = logging.getLogger("tenant_worker_dispatcher")


OWNER_ONLY_RULES_NOTICE = "Only the business owner can change business rules."
RULE_SAVE_FAILED_NOTICE = "Sorry, I couldn't save that rule. Please try again."


class TenantWorkerDispatcher:
    """Dispatches inbound messages to tenant worker threads with crash durability and pre-flight deduplication."""

    def __init__(self, data_root: str = "/app/data/tenants"):
        self.data_root = os.environ.get("TENANTS_DATA_DIR", data_root)
        self.reply_skill = WhatsAppReplySkill()
        self.settings = load_default_settings()

    async def _handle_business_rule(
        self, tenant_id: str, body: str, message_sid: str, is_owner: bool
    ) -> Tuple[Optional[str], int]:
        """Saves a rule the owner teaches. Returns (notice for the reply, number of rules saved)."""
        if not ContextExtractor.looks_like_rule(body):
            return None, 0

        if not is_owner:
            logger.info(f"[RULES] Tenant={tenant_id} non-owner tried to set a rule (message_sid={message_sid}).")
            return OWNER_ONLY_RULES_NOTICE, 0

        try:
            memory = TenantMemoryTree(tenant_id=tenant_id, base_data_dir=self.data_root)
            await memory.initialize()
            node = await ContextExtractor(memory_tree=memory).extract_and_store(
                text=body, source_message_id=message_sid
            )
        except Exception as e:
            logger.error(f"[RULES] Tenant={tenant_id} failed to save rule: {e}", exc_info=True)
            return RULE_SAVE_FAILED_NOTICE, 0

        if not node:
            return None, 0
        logger.info(f"[RULES] Tenant={tenant_id} saved rule {node.id} from message_sid={message_sid}.")
        return f"Saved: {node.content}", 1

    async def process_incoming_message(
        self,
        tenant_id: str,
        from_number: str,
        body: str,
        message_sid: str,
        plan_tier: str = "starter",
        business_name: str = "Business Assistant",
        owner_phone: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Processes an incoming WhatsApp message with crash durability checks."""
        logger.info(
            f"[DISPATCHER] Processing message_sid={message_sid} for tenant={tenant_id} "
            f"from {mask_phone_number(from_number)}"
        )
        db_mgr: Optional[TenantDatabaseManager] = None

        try:
            db_mgr = TenantDatabaseManager(tenant_id=tenant_id, base_dir=self.data_root)

            # Pre-flight check: ensure message is not already completed or actively in progress
            def _check_status() -> Optional[str]:
                with db_mgr._get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT status FROM inbound_messages WHERE message_sid = ?;",
                        (message_sid,),
                    )
                    row = cursor.fetchone()
                    return str(row["status"]) if row else None

            current_status = await asyncio.to_thread(_check_status)
            if current_status == "completed":
                logger.warning(
                    f"[DISPATCHER] Dropping retry for message_sid={message_sid} (already completed)."
                )
                return {"status": "skipped", "reason": "already_completed", "message_sid": message_sid}

            if current_status == "processing":
                logger.warning(
                    f"[DISPATCHER] Dropping concurrent run for message_sid={message_sid} (already processing)."
                )
                return {"status": "skipped", "reason": "already_processing", "message_sid": message_sid}

            is_owner = bool(owner_phone) and normalize_phone_number(from_number) == normalize_phone_number(
                owner_phone
            )

            # Business rules: only the owner can teach them; saved before the AI turn so it can use them
            rule_notice, rules_saved = await self._handle_business_rule(
                tenant_id=tenant_id, body=body, message_sid=message_sid, is_owner=is_owner
            )

            # Initialize and invoke isolated agent loop
            agent = LiteLLMAgent(
                tenant_id=tenant_id,
                plan_tier=plan_tier,
                business_name=business_name,
                base_data_dir=self.data_root,
            )

            result = await agent.process_user_turn(
                message_sid=message_sid,
                from_number=from_number,
                user_message=body,
                reply_skill=self.reply_skill,
                is_owner=is_owner,
                reply_prefix=rule_notice,
            )

            return {
                "status": result.get("status", "completed"),
                "tenant_id": tenant_id,
                "message_sid": message_sid,
                "reply": result.get("reply", ""),
                "rules_extracted": rules_saved,
            }
        except Exception as e:
            logger.error(
                f"[DISPATCHER CRASH] Processing failed for message_sid={message_sid}: {e}",
                exc_info=True,
            )
            fallback_msg = self.settings.get("whatsapp", {}).get(
                "error_fallback_reply",
                "I'm having trouble processing that request right now. Please try again in a moment.",
            )
            try:
                await self.reply_skill.send_reply(to_number=from_number, message=fallback_msg)
            except Exception as send_err:
                logger.error(f"[DISPATCHER FALLBACK FAILED] Could not send fallback reply: {send_err}")
            if db_mgr is not None:
                try:
                    await db_mgr.update_message_status(message_sid, "failed")
                except Exception as status_err:
                    logger.error(f"[DISPATCHER] Could not mark message_sid={message_sid} failed: {status_err}")
            return {
                "status": "failed",
                "tenant_id": tenant_id,
                "message_sid": message_sid,
                "reply": fallback_msg,
                "error": str(e),
            }

    async def replay_pending_messages(
        self,
        tenant_id: str,
        plan_tier: str = "starter",
        business_name: str = "Business Assistant",
        owner_phone: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Recovers unfinished messages after a restart. Resets crashed 'processing' rows to 'pending'."""
        db_mgr = TenantDatabaseManager(tenant_id=tenant_id, base_dir=self.data_root)

        def _reset_and_fetch_pending():
            with db_mgr._get_connection() as conn:
                # 1. Reset messages caught mid-turn when container stopped/crashed
                conn.execute(
                    """
                    UPDATE inbound_messages 
                    SET status = 'pending', updated_at = CURRENT_TIMESTAMP 
                    WHERE status = 'processing';
                    """
                )
                conn.commit()

                # 2. Fetch all pending messages for re-execution
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT message_sid, from_number, body 
                    FROM inbound_messages 
                    WHERE status = 'pending' 
                    ORDER BY created_at ASC;
                    """
                )
                return [dict(row) for row in cursor.fetchall()]

        pending_rows = await asyncio.to_thread(_reset_and_fetch_pending)
        replayed_results = []
        for msg in pending_rows:
            logger.info(
                f"[REPLAY] Replaying recovered pending message {msg['message_sid']} for tenant={tenant_id}"
            )
            res = await self.process_incoming_message(
                tenant_id=tenant_id,
                plan_tier=plan_tier,
                business_name=business_name,
                owner_phone=owner_phone,
                from_number=msg["from_number"],
                body=msg["body"] or "",
                message_sid=msg["message_sid"],
            )
            replayed_results.append(res)
        return replayed_results