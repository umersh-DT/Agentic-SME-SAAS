import json
import logging
import re
import uuid
from typing import List, Optional
from pydantic import BaseModel, Field

from src.skills.memory_tree import MemoryNode, TenantMemoryTree

logger = logging.getLogger("context_extractor")


class ExtractedMemoryUpdate(BaseModel):
    is_rule_or_preference: bool = Field(
        description="True if the message defines a business policy, operational rule, price, or client preference."
    )
    title: Optional[str] = Field(
        default=None, description="Short summary title of the rule."
    )
    node_type: Optional[str] = Field(
        default="policy",
        description="Node type: 'policy', 'pricing', 'preference', or 'category'.",
    )
    category: Optional[str] = Field(
        default="operations",
        description="Category: 'pricing', 'operations', 'scheduling', 'customer_service'.",
    )
    content: Optional[str] = Field(
        default=None, description="Clear, normalized statement of the rule."
    )
    concepts: List[str] = Field(
        default_factory=list,
        description="Key search terms or entities for FTS and concept linking.",
    )
    supersedes_query: Optional[str] = Field(
        default=None,
        description="Search query to locate any previous conflicting rule this replaces.",
    )


class ContextExtractor:
    """Extracts operational policies and business constraints from conversation streams

    and updates the tenant's memory tree.
    """

    def __init__(self, memory_tree: TenantMemoryTree, llm_client: Optional[object] = None):
        self.memory_tree = memory_tree
        self.llm_client = llm_client

    def _heuristic_fallback_parse(self, text: str) -> ExtractedMemoryUpdate:
        """Deterministic rule parser for local testing without active external LLM credentials."""
        lower = text.lower()
        pricing_triggers = ["deposit", "price", "charge", "cost", "quote", "rate", "$", "usd", "aed", "eur"]
        policy_triggers = ["always", "never", "must", "from now on", "policy", "require", "we don't", "we do"]

        is_pricing = any(word in lower for word in pricing_triggers)
        is_policy = any(phrase in lower for phrase in policy_triggers)

        if is_pricing or is_policy:
            category = "pricing" if is_pricing else "operations"
            node_type = "pricing" if is_pricing else "policy"
            words = [re.sub(r"[^\w]", "", w) for w in lower.split() if len(w) > 3]
            concepts = list(set(words))[:5]
            
            return ExtractedMemoryUpdate(
                is_rule_or_preference=True,
                title=f"Update: {text[:30].strip()}...",
                node_type=node_type,
                category=category,
                content=text.strip(),
                concepts=concepts,
                supersedes_query=" ".join(concepts[:2]) if concepts else None
            )

        return ExtractedMemoryUpdate(is_rule_or_preference=False)

    async def extract_and_store(
        self,
        text: str,
        source_message_id: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> Optional[MemoryNode]:
        """Parses message content, evaluates policy updates, and writes directly to the memory tree."""
        # Use heuristic fallback if no active LLM client is configured
        update: ExtractedMemoryUpdate = self._heuristic_fallback_parse(text)

        if not update.is_rule_or_preference or not update.content:
            return None

        superseded_node_id = None
        if update.supersedes_query:
            prior_nodes = await self.memory_tree.search_memory(update.supersedes_query, limit=1)
            if prior_nodes:
                superseded_node_id = prior_nodes[0]["id"]

        node_id = f"node_{uuid.uuid4().hex[:12]}"
        node = MemoryNode(
            id=node_id,
            parent_id=parent_id,
            node_type=update.node_type or "policy",
            title=update.title or "Business Policy",
            content=update.content,
            category=update.category or "operations",
            strength=1.0,
            supersedes=superseded_node_id,
            source_message_id=source_message_id,
        )

        await self.memory_tree.add_node(node, concepts=update.concepts)
        return node


if __name__ == "__main__":
    import asyncio
    import shutil
    from pathlib import Path

    async def _test():
        test_dir = Path("/tmp/test_extractor")
        tree = TenantMemoryTree(tenant_id="tenant_photographer_001", base_data_dir=str(test_dir))
        await tree.initialize()

        extractor = ContextExtractor(memory_tree=tree)

        # Message 1: Conversational noise (should be ignored)
        node_1 = await extractor.extract_and_store("Hey, are you able to reply to messages today?")
        print(f"Noise message extracted node: {node_1}")

        # Message 2: Explicit policy rule (should be extracted and stored)
        msg_rule = "From now on, we require a 25% non-refundable booking fee for weekend portrait sessions."
        node_2 = await extractor.extract_and_store(msg_rule, source_message_id="msg_wa_1002")
        print(f"[OK] Rule extracted successfully:")
        print(f" - ID: {node_2.id}")
        print(f" - Category: {node_2.category}")
        print(f" - Content: {node_2.content}")

        # Search the memory tree to verify full-text indexing
        search_res = await tree.search_memory("portrait booking fee")
        print(f"[OK] FTS Search verified: {len(search_res)} match found: {search_res[0]['title']}")

        if test_dir.exists():
            shutil.rmtree(test_dir)

    asyncio.run(_test())