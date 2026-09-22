import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import aiosqlite


@dataclass
class MemoryNode:
    id: str
    parent_id: Optional[str]
    node_type: str  # e.g., 'category', 'policy', 'preference', 'pricing'
    title: str
    content: str
    category: str
    strength: float = 1.0
    supersedes: Optional[str] = None
    source_message_id: Optional[str] = None
    created_at: Optional[str] = None


class TenantMemoryTree:
    """Manages an isolated SQLite Hierarchical Memory Tree with FTS5 search

    for an individual tenant.
    """

    def __init__(self, tenant_id: str, base_data_dir: str = "data/tenants"):
        self.tenant_id = tenant_id
        self.data_dir = Path(base_data_dir)
        self.db_path = self.data_dir / f"{tenant_id}.sqlite"

    async def initialize(self) -> None:
        """Initializes tables, triggers, and full-text search virtual index."""
        self.data_dir.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode = WAL;")
            await db.execute("PRAGMA foreign_keys = ON;")

            # 1. Main Hierarchical Memory Nodes Table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS memory_nodes (
                    id TEXT PRIMARY KEY,
                    parent_id TEXT NULL,
                    node_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    category TEXT NOT NULL,
                    strength REAL DEFAULT 1.0,
                    supersedes TEXT NULL,
                    source_message_id TEXT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (parent_id) REFERENCES memory_nodes (id) ON DELETE CASCADE
                );
            """)

            # 2. Concept Tags Table for Semantic Keyword Matching
            await db.execute("""
                CREATE TABLE IF NOT EXISTS memory_concepts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT NOT NULL,
                    concept TEXT NOT NULL,
                    relevance_score REAL DEFAULT 1.0,
                    FOREIGN KEY (node_id) REFERENCES memory_nodes (id) ON DELETE CASCADE
                );
            """)

            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_concept_lookup ON"
                " memory_concepts (concept);"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_node_parent ON memory_nodes"
                " (parent_id);"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_node_category ON memory_nodes"
                " (category);"
            )

            # 3. FTS5 Virtual Table for Fast Lexical Retrieval
            await db.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    node_id UNINDEXED,
                    title,
                    content,
                    tokenize = 'porter unicode61'
                );
            """)

            # Triggers to keep FTS index synchronized on INSERT and DELETE
            await db.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_memory_nodes_ai AFTER INSERT ON memory_nodes BEGIN
                    INSERT INTO memory_fts(node_id, title, content)
                    VALUES (new.id, new.title, new.content);
                END;
            """)

            await db.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_memory_nodes_ad AFTER DELETE ON memory_nodes BEGIN
                    DELETE FROM memory_fts WHERE node_id = old.id;
                END;
            """)

            await db.commit()

    async def add_node(
        self,
        node: MemoryNode,
        concepts: Optional[List[str]] = None,
    ) -> str:
        """Inserts or updates a memory node and records related concepts."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            await db.execute(
                """
                INSERT OR REPLACE INTO memory_nodes 
                (id, parent_id, node_type, title, content, category, strength, supersedes, source_message_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
                (
                    node.id,
                    node.parent_id,
                    node.node_type,
                    node.title,
                    node.content,
                    node.category,
                    node.strength,
                    node.supersedes,
                    node.source_message_id,
                ),
            )

            if concepts:
                for concept in concepts:
                    await db.execute(
                        """
                        INSERT INTO memory_concepts (node_id, concept, relevance_score)
                        VALUES (?, ?, 1.0)
                    """,
                        (node.id, concept.strip().lower()),
                    )

            await db.commit()
            return node.id

    async def search_memory(
        self, query: str, limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Executes full-text lexical search and returns matching nodes ranked by BM25."""
        clean_query = "".join(
            c for c in query if c.isalnum() or c.isspace()
        ).strip()
        if not clean_query:
            return []

        # Convert whitespace-delimited words into FTS query (e.g. "curtain deposit" -> "curtain OR deposit")
        terms = clean_query.split()
        fts_expr = " OR ".join(f'"{term}"*' for term in terms)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT m.id, m.parent_id, m.node_type, m.title, m.content, m.category, m.strength
                FROM memory_fts f
                JOIN memory_nodes m ON f.node_id = m.id
                WHERE memory_fts MATCH ?
                ORDER BY bm25(memory_fts) ASC
                LIMIT ?
            """,
                (fts_expr, limit),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_node_by_id(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a single node by its unique identifier."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM memory_nodes WHERE id = ?", (node_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_children(self, parent_id: str) -> List[Dict[str, Any]]:
        """Returns all child nodes under a given category or parent node."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM memory_nodes WHERE parent_id = ? ORDER BY created_at ASC",
                (parent_id,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


if __name__ == "__main__":
    async def _test():
        test_dir = Path("/tmp/test_tenants")
        tree = TenantMemoryTree(
            tenant_id="tenant_curtains_001", base_data_dir=str(test_dir)
        )
        await tree.initialize()

        # 1. Insert Root Node
        root = MemoryNode(
            id="policy_root",
            parent_id=None,
            node_type="category",
            title="General Business Policies",
            content="Top-level guidelines for orders and customer service.",
            category="operations",
        )
        await tree.add_node(root)

        # 2. Insert Child Policy Node
        child = MemoryNode(
            id="deposit_policy_01",
            parent_id="policy_root",
            node_type="policy",
            title="Deposit & Payment Policy",
            content=(
                "All custom curtain orders require a mandatory 50% upfront"
                " deposit before cutting fabric."
            ),
            category="pricing",
        )
        await tree.add_node(
            child, concepts=["deposit", "custom curtains", "fabric cut"]
        )

        # 3. Test FTS Search
        results = await tree.search_memory("curtain deposit")
        print(f"[OK] Memory Tree initialized. Found {len(results)} matches:")
        for res in results:
            print(f" - [{res['node_type'].upper()}] {res['title']}: {res['content']}")

        # 4. Clean up test dir
        import shutil
        if test_dir.exists():
            shutil.rmtree(test_dir)

    asyncio.run(_test())