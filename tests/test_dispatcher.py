import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.storage_models import TenantDatabaseManager
from src.gateway.dispatcher import TenantWorkerDispatcher


class TestTenantWorkerDispatcher(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dispatcher = TenantWorkerDispatcher(data_root=self.temp_dir.name)
        self.tenant_id = "tenant_unit_test_01"
        self.db_mgr = TenantDatabaseManager(tenant_id=self.tenant_id, base_dir=self.temp_dir.name)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_process_incoming_message_hermetic(self):
        """Verifies background processing extracts rules and stores context hermetically."""
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.tool_calls = None
        mock_resp.choices[0].message.content = "Understood. I have recorded your cancellation policy."
        mock_resp.choices[0].message.to_dict.return_value = {
            "role": "assistant",
            "content": "Understood. I have recorded your cancellation policy.",
        }
        mock_resp.usage.prompt_tokens = 45
        mock_resp.usage.completion_tokens = 15

        with patch("litellm.acompletion", return_value=mock_resp), \
             patch.object(self.dispatcher.reply_skill, "send_reply", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"success": True, "message_sid": "SM_mock_reply"}

            result = await self.dispatcher.process_incoming_message(
                tenant_id=self.tenant_id,
                from_number="+971501234567",
                body="Remember our standard cancellation policy is 24 hours in advance.",
                message_sid="SM_unit_test_sid",
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tenant_id"], self.tenant_id)
        self.assertEqual(result["message_sid"], "SM_unit_test_sid")
        self.assertEqual(result["rules_extracted"], 1)
        mock_send.assert_called_once()

    async def test_startup_replay_resets_processing_and_replays_message(self):
        """Fix 2 Verification: A message trapped in 'processing' mid-crash is reset and replayed."""
        # 1. Seed a message stuck in 'processing' as if the container crashed mid-turn
        message_sid = "SM_crashed_mid_turn_999"
        await self.db_mgr.persist_inbound_message(
            message_sid=message_sid,
            from_number="+971501234567",
            body="What are your standard showroom opening hours?",
            num_media=0,
        )
        await self.db_mgr.update_message_status(message_sid, "processing")

        # Confirm status is 'processing'
        with self.db_mgr._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM inbound_messages WHERE message_sid = ?;", (message_sid,))
            row = cursor.fetchone()
            self.assertEqual(row["status"], "processing")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.tool_calls = None
        mock_resp.choices[0].message.content = "Our showroom is open Monday through Saturday, 9 AM to 8 PM."
        mock_resp.choices[0].message.to_dict.return_value = {
            "role": "assistant",
            "content": "Our showroom is open Monday through Saturday, 9 AM to 8 PM.",
        }
        mock_resp.usage.prompt_tokens = 50
        mock_resp.usage.completion_tokens = 20

        with patch("litellm.acompletion", return_value=mock_resp), \
             patch.object(self.dispatcher.reply_skill, "send_reply", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"success": True, "message_sid": "SM_replayed_reply"}

            # 2. Trigger startup replay routine
            replayed = await self.dispatcher.replay_pending_messages(
                tenant_id=self.tenant_id,
                plan_tier="starter",
                business_name="Luxe Curtain Interiors",
            )

        # 3. Assert the crashed message was picked up and completed
        self.assertEqual(len(replayed), 1)
        self.assertEqual(replayed[0]["message_sid"], message_sid)
        self.assertEqual(replayed[0]["status"], "completed")

        # 4. Confirm message status is now 'completed' in SQLite
        with self.db_mgr._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM inbound_messages WHERE message_sid = ?;", (message_sid,))
            row = cursor.fetchone()
            self.assertEqual(row["status"], "completed")


if __name__ == "__main__":
    unittest.main()