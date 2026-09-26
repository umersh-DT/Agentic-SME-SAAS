import os
import tempfile
import unittest

from src.gateway.dispatcher import TenantWorkerDispatcher


class TestTenantWorkerDispatcher(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dispatcher = TenantWorkerDispatcher(data_root=self.temp_dir.name)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_process_incoming_message_hermetic(self):
        """Verifies background processing extracts rules and stores context without sending fake autoreplies."""
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
        self.assertNotIn("reply_dispatched", result)


if __name__ == "__main__":
    unittest.main()