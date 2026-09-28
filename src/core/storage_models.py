import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
import yaml

from src.utils.security import get_safe_tenant_storage_path

logger = logging.getLogger("tenant_storage_models")


def load_default_settings(config_path: str = "config/default_settings.yaml") -> Dict[str, Any]:
    """Loads default configuration settings with fallback defaults."""
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {
        "llm": {"model": "openai/gpt-4o-mini", "timeout_seconds": 25, "max_tokens": 800, "max_tool_rounds": 4},
        "quotas_usd_monthly": {"starter": 5.00, "pro": 20.00, "enterprise": 100.00},
        "pricing_per_1m_tokens": {"prompt_usd": 0.150, "completion_usd": 0.600},
        "whatsapp": {"max_message_chars": 1550, "history_limit": 10},
    }


class TenantDatabaseManager:
    """Manages durability, conversation history, and token metering in tenant SQLite database."""

    def __init__(self, tenant_id: str, base_dir: str = "/app/data/tenants"):
        self.tenant_id = tenant_id
        self.db_path = get_safe_tenant_storage_path(tenant_id, base_dir=base_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_tables(self) -> None:
        """Initializes tables for durability, conversation memory, and token metering."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # 1. Inbound message durability table (crash replay safe)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS inbound_messages (
                    message_sid TEXT PRIMARY KEY,
                    from_number TEXT NOT NULL,
                    body TEXT,
                    num_media INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending', -- pending, processing, completed, failed
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 2. Rolling conversation history table (per-tenant session buffer)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS conversation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_sid TEXT,
                    role TEXT NOT NULL, -- 'user', 'assistant', 'system'
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_conv_history_created 
                ON conversation_history(created_at);
            """)

            # 3. Token & monthly USD cost metering table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS token_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_sid TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_token_usage_created 
                ON token_usage(created_at);
            """)
            conn.commit()

    # --- Inbound Durability Operations ---

    def _sync_persist_inbound_message(
        self,
        message_sid: str,
        from_number: str,
        body: str,
        num_media: int = 0,
    ) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO inbound_messages (message_sid, from_number, body, num_media, status)
                VALUES (?, ?, ?, ?, 'pending');
                """,
                (message_sid, from_number, body, num_media),
            )
            conn.commit()

    async def persist_inbound_message(
        self,
        message_sid: str,
        from_number: str,
        body: str,
        num_media: int = 0,
    ) -> None:
        """Persists message before acknowledging Twilio webhook."""
        await asyncio.to_thread(self._sync_persist_inbound_message, message_sid, from_number, body, num_media)

    def _sync_update_message_status(self, message_sid: str, status: str) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE inbound_messages 
                SET status = ?, updated_at = CURRENT_TIMESTAMP 
                WHERE message_sid = ?;
                """,
                (status, message_sid),
            )
            conn.commit()

    async def update_message_status(self, message_sid: str, status: str) -> None:
        await asyncio.to_thread(self._sync_update_message_status, message_sid, status)

    # --- Conversation History Operations ---

    def _sync_get_recent_history(self, limit: int = 10) -> List[Dict[str, str]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT role, content 
                FROM conversation_history 
                ORDER BY id DESC 
                LIMIT ?;
                """,
                (limit,),
            )
            rows = cursor.fetchall()
            # Reverse to maintain chronological order [oldest ... newest]
            return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    async def get_recent_history(self, limit: int = 10) -> List[Dict[str, str]]:
        return await asyncio.to_thread(self._sync_get_recent_history, limit)

    def _sync_append_history(self, message_sid: str, role: str, content: str) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO conversation_history (message_sid, role, content)
                VALUES (?, ?, ?);
                """,
                (message_sid, role, content),
            )
            conn.commit()

    async def append_history(self, message_sid: str, role: str, content: str) -> None:
        await asyncio.to_thread(self._sync_append_history, message_sid, role, content)

    # --- Token Metering & USD Cost Cap Enforcement ---

    def _sync_get_current_month_cost(self) -> float:
        """Calculates total spend for the current calendar month in UTC."""
        now = datetime.now(timezone.utc)
        start_of_month = f"{now.year:04d}-{now.month:02d}-01 00:00:00"

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COALESCE(SUM(cost_usd), 0.0) as total_spent
                FROM token_usage
                WHERE created_at >= ?;
                """,
                (start_of_month,),
            )
            row = cursor.fetchone()
            return float(row["total_spent"]) if row else 0.0

    async def get_current_month_cost(self) -> float:
        return await asyncio.to_thread(self._sync_get_current_month_cost)

    def _sync_record_token_usage(
        self,
        message_sid: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        prompt_rate_per_1m: float,
        completion_rate_per_1m: float,
    ) -> float:
        total_tokens = prompt_tokens + completion_tokens
        cost_usd = (prompt_tokens * (prompt_rate_per_1m / 1_000_000.0)) + (
            completion_tokens * (completion_rate_per_1m / 1_000_000.0)
        )

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO token_usage (message_sid, model, prompt_tokens, completion_tokens, total_tokens, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (message_sid, model, prompt_tokens, completion_tokens, total_tokens, cost_usd),
            )
            conn.commit()
        return cost_usd

    async def record_token_usage(
        self,
        message_sid: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        prompt_rate_per_1m: float = 0.150,
        completion_rate_per_1m: float = 0.600,
    ) -> float:
        return await asyncio.to_thread(
            self._sync_record_token_usage,
            message_sid,
            model,
            prompt_tokens,
            completion_tokens,
            prompt_rate_per_1m,
            completion_rate_per_1m,
        )

    async def check_quota_exceeded(self, plan_tier: str, settings: Dict[str, Any]) -> Tuple[bool, float, float]:
        """Checks if tenant has exceeded monthly USD quota.
        
        Returns:
            Tuple[bool, float, float]: (is_exceeded, current_spend, max_quota)
        """
        quotas = settings.get("quotas_usd_monthly", {"starter": 5.0, "pro": 20.0, "enterprise": 100.0})
        max_quota = float(quotas.get(plan_tier, quotas.get("starter", 5.0)))
        current_spend = await self.get_current_month_cost()
        return (current_spend >= max_quota, current_spend, max_quota)