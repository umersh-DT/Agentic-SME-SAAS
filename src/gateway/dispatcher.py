import logging
import os
from pathlib import Path
from typing import Any, Dict

from src.core.context_extractor import ContextExtractor
from src.skills.memory_tree import TenantMemoryTree
from src.skills.whatsapp_reply import WhatsAppReplySkill

logger = logging.getLogger("worker_dispatcher")


class TenantWorkerDispatcher:
    """Dispatches incoming WhatsApp messages asynchronously into tenant-isolated pipelines."""

    def __init__(self, data_root: str = "/app/data/tenants"):
        self.data_root = data_root if Path(data_root).exists() else "data/tenants"
        Path(self.data_root).mkdir(parents=True, exist_ok=True)
        self.reply_skill = WhatsAppReplySkill()

    async def process_incoming_message(
        self,
        tenant_id: str,
        from_number: str,
        body: str,
        message_sid: str,
        send_reply: bool = True,
    ) -> Dict[str, Any]:
        """Asynchronously executes context extraction, rule indexing, and memory retrieval."""
        logger.info(f"[DISPATCH] Starting background task for MessageSid={message_sid} (Tenant: {tenant_id})")

        # 1. Initialize tenant memory tree
        memory = TenantMemoryTree(tenant_id=tenant_id, base_data_dir=self.data_root)
        await memory.initialize()

        # 2. Bind ContextExtractor to tenant memory tree instance
        extractor = ContextExtractor(memory_tree=memory)

        # 3. Extract and store operational rules
        extracted_node = await extractor.extract_and_store(
            text=body,
            source_message_id=message_sid,
        )
        rules_stored = 1 if extracted_node is not None else 0

        # 4. Retrieve relevant tenant context using FTS5 search
        relevant_context = await memory.search_memory(query=body, limit=3)

        # 5. Outbound WhatsApp Reply Generation
        reply_result = None
        if send_reply and os.getenv("TWILIO_ACCOUNT_SID"):
            reply_text = (
                f"Thank you for contacting us. We noted your request: '{body[:50]}'. "
                f"Our team will follow up promptly."
            )
            reply_result = await self.reply_skill.send_message(
                to_number=from_number,
                body=reply_text,
            )

        result = {
            "status": "completed",
            "tenant_id": tenant_id,
            "message_sid": message_sid,
            "from_number": from_number,
            "rules_extracted": rules_stored,
            "context_hits": len(relevant_context),
            "reply_dispatched": reply_result.get("success", False) if reply_result else False,
        }
        logger.info(f"[DISPATCH SUCCESS] MessageSid={message_sid} processed | Stored rules: {rules_stored}")
        return result


dispatcher = TenantWorkerDispatcher()