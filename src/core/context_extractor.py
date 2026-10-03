import logging
import re
import uuid
from typing import List, Optional
from pydantic import BaseModel, Field

from src.skills.memory_tree import MemoryNode, TenantMemoryTree

logger = logging.getLogger("context_extractor")


# Phrases that signal the owner is teaching the assistant a business rule.
# Matched on word boundaries against the lower-cased message.
TEACHING_CUES = [
    r"remember",
    r"from now on",
    r"going forward",
    r"note that",
    r"policy",
    r"new rule",
    r"rule is",
    r"we always",
    r"we never",
    r"we don'?t",
    r"we do not",
    r"we require",
    r"we charge",
    r"we only",
    r"our (?:price|prices|rate|rates|hours|deposit|fee|fees|terms)",
    r"requires?",
    r"required",
    r"must",
    r"mandatory",
    r"non-refundable",
    r"no refunds?",
    r"^always",
    r"^never",
]
_TEACHING_RE = re.compile(r"\b(?:" + "|".join(TEACHING_CUES) + r")\b")

# Messages starting with these words are questions or commands, not rules.
NON_RULE_FIRST_WORDS = {
    "what", "when", "where", "who", "whom", "why", "how", "which",
    "can", "could", "do", "does", "did", "is", "are", "was", "should", "would", "will",
    "invoice", "create", "make", "draft", "approve", "send", "generate", "bill",
    "remind", "tell", "show", "list", "find", "search", "check", "give", "explain",
}

# Leading filler removed before storing the rule text.
_LEADING_FILLER_RE = re.compile(
    r"^(?:please\s+)?(?:(?:remember|note)\b(?:\s+that)?\s*[:,\-]?\s*)?", re.IGNORECASE
)

_STOPWORDS = {
    "that", "this", "with", "from", "have", "will", "your", "their", "they", "them", "were",
    "been", "into", "only", "also", "than", "then", "when", "what", "remember", "please", "always",
    "never", "dont", "does", "should", "must", "every", "there", "these", "those", "about",
}


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


class ContextExtractor:
    """Recognises business rules the owner teaches over WhatsApp and stores them in the tenant's memory tree.

    Detection is deterministic (keyword cues), so it costs nothing and behaves predictably:
    questions and invoice/commands are never stored as rules.
    """

    def __init__(self, memory_tree: TenantMemoryTree, llm_client: Optional[object] = None):
        self.memory_tree = memory_tree
        self.llm_client = llm_client

    @staticmethod
    def looks_like_rule(text: str) -> bool:
        """True if the message reads as the sender teaching a business rule."""
        lower = (text or "").strip().lower()
        if not lower or lower.endswith("?"):
            return False
        lower = re.sub(r"^please\s+", "", lower)
        first_word = re.split(r"[^a-z']+", lower, maxsplit=1)[0]
        if first_word in NON_RULE_FIRST_WORDS:
            return False
        return bool(_TEACHING_RE.search(lower))

    def parse(self, text: str) -> ExtractedMemoryUpdate:
        """Turns a teaching message into a normalized memory update."""
        if not self.looks_like_rule(text):
            return ExtractedMemoryUpdate(is_rule_or_preference=False)
        return self.normalize_rule(text)

    @staticmethod
    def normalize_rule(text: str) -> ExtractedMemoryUpdate:
        """Normalizes rule text (used for new rules and for 'change rule N: ...')."""
        content = _LEADING_FILLER_RE.sub("", text.strip(), count=1).strip() or text.strip()
        content = content[0].upper() + content[1:]

        lower = content.lower()
        pricing_words = ["deposit", "price", "charge", "cost", "quote", "rate", "fee", "aed", "usd", "$", "%"]
        is_pricing = any(word in lower for word in pricing_words)

        words = [re.sub(r"[^\w]", "", w) for w in lower.split()]
        concepts = list(dict.fromkeys(w for w in words if len(w) > 3 and w not in _STOPWORDS))[:8]

        return ExtractedMemoryUpdate(
            is_rule_or_preference=True,
            title=content if len(content) <= 60 else f"{content[:57].rstrip()}...",
            node_type="pricing" if is_pricing else "policy",
            category="pricing" if is_pricing else "operations",
            content=content,
            concepts=concepts,
        )

    async def extract_and_store(
        self,
        text: str,
        source_message_id: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> Optional[MemoryNode]:
        """Stores the rule in the message, if any. Re-processing the same message returns the saved rule."""
        update = self.parse(text)
        if not update.is_rule_or_preference or not update.content:
            return None

        if source_message_id:
            existing = await self.memory_tree.get_node_by_source_message(source_message_id)
            if existing:
                logger.info(f"[CONTEXT EXTRACTOR] Rule for message {source_message_id} already saved; skipping.")
                return MemoryNode(
                    id=existing["id"],
                    parent_id=existing["parent_id"],
                    node_type=existing["node_type"],
                    title=existing["title"],
                    content=existing["content"],
                    category=existing["category"],
                    strength=existing["strength"],
                    supersedes=existing["supersedes"],
                    source_message_id=existing["source_message_id"],
                    created_at=existing["created_at"],
                )

        node = MemoryNode(
            id=f"node_{uuid.uuid4().hex[:12]}",
            parent_id=parent_id,
            node_type=update.node_type or "policy",
            title=update.title or "Business Policy",
            content=update.content,
            category=update.category or "operations",
            strength=1.0,
            supersedes=None,
            source_message_id=source_message_id,
        )

        await self.memory_tree.add_node(node, concepts=update.concepts)
        return node
