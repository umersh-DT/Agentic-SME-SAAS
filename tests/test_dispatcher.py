import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.gateway.dispatcher import TenantWorkerDispatcher


class TestTenantWorkerDispatcher(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dispatcher = TenantWorkerDispatcher(data_root=self.temp_dir.name)

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
                tenant_id="tenant_unit_test_01",
                from_number="+971501234567",
                body="Remember our standard cancellation policy is 24 hours in advance.",
                message_sid="SM_unit_test_sid",
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tenant_id"], "tenant_unit_test_01")
        self.assertEqual(result["message_sid"], "SM_unit_test_sid")
        self.assertEqual(result["rules_extracted"], 1)
        mock_send.assert_called_once()


if __name__ == "__main__":
    unittest.main()