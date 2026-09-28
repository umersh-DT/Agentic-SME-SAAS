import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.agent_loop import LiteLLMAgent
from src.core.context_extractor import ContextExtractor
from src.core.storage_models import TenantDatabaseManager, load_default_settings
from src.skills.memory_tree import TenantMemoryTree
from src.skills.whatsapp_reply import WhatsAppReplySkill

logger = logging.getLogger("worker_dispatcher")


class TenantWorkerDispatcher:
    """Asynchronously coordinates durability checks, context extraction, agent execution, and crash recovery."""

    def __init__(self, data_root: str = "/app/data/tenants"):
        self.data_root = data_root if Path(data_root).exists() else "data/tenants"
        Path(self.data_root).mkdir(parents=True, exist_ok=True)
        self.reply_skill = WhatsAppReplySkill()
        self.settings = load_default_settings()

    async def _get_message_status(self, db_mgr: TenantDatabaseManager, message_sid: str) -> Optional[str]:
        """Queries the current processing status of an inbound message in SQLite."""
        def _sync_get_status():
            with db_mgr._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT status FROM inbound_messages WHERE message_sid = ?;",
                    (message_sid,),
                )
                row = cursor.fetchone()
                return row["status"] if row else None

        return await asyncio.to_thread(_sync_get_status)

    async def process_incoming_message(
        self,
        tenant_id: str,
        from_number: str,
        body: str,
        message_sid: str,
        plan_tier: str = "starter",
        business_name: str = "Business Assistant",
    ) -> Dict[str, Any]:
        """Processes message with database pre-flight status check, context extraction, and agent execution."""
        logger.info(
            f"[DISPATCH] Starting background processing MessageSid={message_sid} (Tenant: {tenant_id})"
        )

        db_mgr = TenantDatabaseManager(tenant_id=tenant_id, base_dir=self.data_root)

        # 1. Crash safety pre-flight check: Skip if already processing or completed
        current_status = await self._get_message_status(db_mgr, message_sid)
        if current_status in ("processing", "completed"):
            logger.warning(
                f"[DISPATCH DEDUP] MessageSid={message_sid} is already '{current_status}'. Skipping to prevent duplicate turn."
            )
            return {
                "status": "skipped",
                "reason": f"already_{current_status}",
                "tenant_id": tenant_id,
                "message_sid": message_sid,
            }

        fallback_msg = self.settings.get("whatsapp", {}).get(
            "error_fallback_reply",
            "I'm having trouble processing that request right now. Please try again in a moment.",
        )

        rules_count = 0
        try:
            # 2. Context Extraction (Business Rules)
            try:
                memory = TenantMemoryTree(tenant_id=tenant_id, base_data_dir=self.data_root)
                await memory.initialize()
                extractor = ContextExtractor(memory_tree=memory)
                extracted = await extractor.extract_and_store(text=body, source_message_id=message_sid)
                if isinstance(extracted, list):
                    rules_count = len(extracted)
                elif isinstance(extracted, int):
                    rules_count = extracted
                else:
                    rules_count = 1 if extracted else 0
            except Exception as e:
                logger.warning(f"[DISPATCH CONTEXT EXTRACTOR] Non-fatal extraction warning: {e}")

            # 3. Agent Execution
            agent = LiteLLMAgent(
                tenant_id=tenant_id,
                plan_tier=plan_tier,
                business_name=business_name,
                base_data_dir=self.data_root,
            )

            res = await agent.process_user_turn(
                message_sid=message_sid,
                from_number=from_number,
                user_message=body,
                reply_skill=self.reply_skill,
            )

            res["tenant_id"] = tenant_id
            res["message_sid"] = message_sid
            res["rules_extracted"] = rules_count
            return res

        except Exception as e:
            # Top-level turn error safety net
            logger.exception(f"[DISPATCH FATAL ERROR] MessageSid={message_sid} failed turn: {e}")
            try:
                await self.reply_skill.send_reply(to_number=from_number, message=fallback_msg)
            except Exception as reply_err:
                logger.error(f"[DISPATCH FALLBACK FAILED] Could not send fallback: {reply_err}")

            await db_mgr.update_message_status(message_sid, "failed")
            return {
                "status": "failed",
                "error": str(e),
                "tenant_id": tenant_id,
                "message_sid": message_sid,
                "rules_extracted": rules_count,
            }

    async def replay_pending_messages(
        self,
        tenant_id: str,
        plan_tier: str = "starter",
        business_name: str = "Business Assistant",
    ) -> List[Dict[str, Any]]:
        """Scans database for pending messages left unfinished after a crash or restart, and replays them."""
        db_mgr = TenantDatabaseManager(tenant_id=tenant_id, base_dir=self.data_root)

        def _sync_find_pending():
            with db_mgr._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT message_sid, from_number, body 
                    FROM inbound_messages 
                    WHERE status = 'pending' 
                    ORDER BY created_at ASC;
                    """
                )
                return [dict(r) for r in cursor.fetchall()]

        pending_rows = await asyncio.to_thread(_sync_find_pending)
        results = []
        for msg in pending_rows:
            logger.info(f"[REPLAY] Replaying pending MessageSid={msg['message_sid']} for tenant={tenant_id}")
            res = await self.process_incoming_message(
                tenant_id=tenant_id,
                from_number=msg["from_number"],
                body=msg.get("body") or "",
                message_sid=msg["message_sid"],
                plan_tier=plan_tier,
                business_name=business_name,
            )
            results.append(res)
        return results