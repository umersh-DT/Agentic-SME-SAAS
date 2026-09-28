import logging
from pathlib import Path
from typing import Any, Dict

from src.core.agent_loop import LiteLLMAgent
from src.core.context_extractor import ContextExtractor
from src.skills.memory_tree import TenantMemoryTree
from src.skills.whatsapp_reply import WhatsAppReplySkill

logger = logging.getLogger("worker_dispatcher")


class TenantWorkerDispatcher:
    """Asynchronously coordinates context extraction, agent loop, and replies for tenants."""

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
        plan_tier: str = "starter",
        business_name: str = "Business Assistant",
    ) -> Dict[str, Any]:
        """Processes message: extracts operational facts, invokes LiteLLM loop, and replies."""
        logger.info(
            f"[DISPATCH] Starting background processing MessageSid={message_sid} (Tenant: {tenant_id})"
        )

        rules_count = 0
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

        # Populate expected test assertions
        res["tenant_id"] = tenant_id
        res["message_sid"] = message_sid
        res["rules_extracted"] = rules_count
        return res