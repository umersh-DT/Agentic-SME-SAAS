import os
import tempfile
import unittest

from src.core.storage_models import TenantDatabaseManager, load_default_settings


class TestTenantDatabaseManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tenant_id = "tenant_test_durability_001"
        self.db_manager = TenantDatabaseManager(tenant_id=self.tenant_id, base_dir=self.temp_dir.name)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_persist_inbound_message_durability(self):
        message_sid = "SM_test_durability_sid_123"
        await self.db_manager.persist_inbound_message(
            message_sid=message_sid,
            from_number="+971501234567",
            body="Durability check message",
            num_media=0,
        )

        with self.db_manager._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status, body FROM inbound_messages WHERE message_sid = ?", (message_sid,))
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["body"], "Durability check message")

        # Update status to completed
        await self.db_manager.update_message_status(message_sid, "completed")
        with self.db_manager._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM inbound_messages WHERE message_sid = ?", (message_sid,))
            self.assertEqual(cursor.fetchone()["status"], "completed")

    async def test_rolling_conversation_history(self):
        # Insert 15 turns
        for i in range(15):
            role = "user" if i % 2 == 0 else "assistant"
            await self.db_manager.append_history(
                message_sid=f"SM_turn_{i}",
                role=role,
                content=f"Message turn number {i}",
            )

        # Retrieve last 10 turns
        history = await self.db_manager.get_recent_history(limit=10)
        self.assertEqual(len(history), 10)
        # Verify chronological order (turn 5 through turn 14)
        self.assertEqual(history[0]["content"], "Message turn number 5")
        self.assertEqual(history[-1]["content"], "Message turn number 14")

    async def test_token_metering_and_usd_quota_guard(self):
        settings = {"quotas_usd_monthly": {"starter": 0.05}}

        # Record usage below cap
        cost = await self.db_manager.record_token_usage(
            message_sid="SM_usage_1",
            model="openai/gpt-4o-mini",
            prompt_tokens=10_000,
            completion_tokens=5_000,
            prompt_rate_per_1m=0.150,
            completion_rate_per_1m=0.600,
        )
        self.assertAlmostEqual(cost, 0.0045, places=5)

        is_exceeded, spend, quota = await self.db_manager.check_quota_exceeded("starter", settings)
        self.assertFalse(is_exceeded)
        self.assertAlmostEqual(spend, 0.0045, places=4)

        # Record second usage pushing spend over 0.05 cap
        await self.db_manager.record_token_usage(
            message_sid="SM_usage_2",
            model="openai/gpt-4o-mini",
            prompt_tokens=200_000,
            completion_tokens=50_000,
            prompt_rate_per_1m=0.150,
            completion_rate_per_1m=0.600,
        )
        is_exceeded, spend, quota = await self.db_manager.check_quota_exceeded("starter", settings)
        self.assertTrue(is_exceeded)
        self.assertGreater(spend, quota)


if __name__ == "__main__":
    unittest.main()