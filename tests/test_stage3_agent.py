import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import os
import tempfile
import unittest

from src.core.agent_loop import LiteLLMAgent
from src.core.storage_models import TenantDatabaseManager
from src.skills.tools_registry import get_scoped_tools
from src.skills.whatsapp_reply import WhatsAppReplySkill, split_message_text


class TestStage3AgentLoop(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tenant_id = "tenant_curtains_001"
        self.base_data_dir = self.temp_dir.name
        self.db_manager = TenantDatabaseManager(tenant_id=self.tenant_id, base_dir=self.base_data_dir)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    def test_message_splitting_1550_chars(self):
        short_text = "Hello from WhatsApp!"
        self.assertEqual(split_message_text(short_text, max_chars=1550), [short_text])

        # Create large text exceeding 3000 chars
        para1 = "Paragraph 1: " + ("A" * 1200)
        para2 = "Paragraph 2: " + ("B" * 1200)
        para3 = "Paragraph 3: " + ("C" * 1200)
        combined = f"{para1}\n\n{para2}\n\n{para3}"

        chunks = split_message_text(combined, max_chars=1550)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 1550)

    def test_tools_tenant_id_not_exposed_in_schema(self):
        """Verifies tenant_id is NEVER in tool schema but present in bound callables."""
        schema, callables = get_scoped_tools(tenant_id=self.tenant_id, base_data_dir=self.base_data_dir)

        for tool in schema:
            fn_params = tool["function"]["parameters"]["properties"]
            self.assertNotIn("tenant_id", fn_params, "Security violation: tenant_id exposed to LLM!")

        self.assertIn("create_invoice", callables)
        self.assertIn("search_business_memory", callables)

    async def test_create_invoice_tool_saves_draft_in_aed(self):
        """Verifies create_invoice returns AED, contains 'draft', and persists record in SQLite."""
        schema, callables = get_scoped_tools(tenant_id=self.tenant_id, base_data_dir=self.base_data_dir)
        create_invoice_fn = callables["create_invoice"]

        # Call scoped invoice tool callable directly
        result = await create_invoice_fn(
            customer_name="Ali Al-Maktoum",
            amount=2500.00,
            description="Custom Motorized Blackout Curtains",
            deposit_percentage=50.0,
            client_contact="+971509988776",
        )

        # 1. Verify currency is AED and status clearly states draft
        self.assertIn("AED", result)
        self.assertIn("draft", result.lower())
        self.assertIn("Draft invoice — not sent to client", result)
        self.assertIn("Ali Al-Maktoum", result)

        # 2. Verify draft record is saved in tenant SQLite database
        saved_draft = None
        with self.db_manager._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM invoices WHERE client_name = ?;", ("Ali Al-Maktoum",))
            row = cursor.fetchone()
            if row:
                saved_draft = dict(row)

        self.assertIsNotNone(saved_draft, "Invoice draft was not saved in SQLite!")
        self.assertEqual(saved_draft["currency"], "AED")
        self.assertEqual(saved_draft["status"], "draft")
        self.assertEqual(saved_draft["tenant_id"], self.tenant_id)
        self.assertAlmostEqual(saved_draft["subtotal"], 2500.00)
        self.assertAlmostEqual(saved_draft["tax_amount"], 125.00)  # 5% UAE VAT
        self.assertAlmostEqual(saved_draft["grand_total"], 2625.00)
        self.assertAlmostEqual(saved_draft["required_deposit"], 1312.50)  # 50% deposit

    async def test_agent_turn_with_invoice_tool_and_token_metering(self):
        """Simulates full LLM tool-calling cycle with LiteLLM mocked."""
        agent = LiteLLMAgent(
            tenant_id=self.tenant_id,
            plan_tier="pro",
            business_name="Luxe Curtain Interiors",
            base_data_dir=self.base_data_dir,
        )

        mock_reply_skill = MagicMock(spec=WhatsAppReplySkill)
        mock_reply_skill.send_reply = AsyncMock(return_value={"status": "sent"})

        # Mock LiteLLM responses: Round 1 emits tool call, Round 2 emits final answer
        tool_call_obj = MagicMock()
        tool_call_obj.id = "call_abc123"
        tool_call_obj.function.name = "create_invoice"
        tool_call_obj.function.arguments = '{"customer_name": "Sarah Connor", "amount": 450.00, "description": "Velvet Drapes"}'

        mock_resp_1 = MagicMock()
        mock_resp_1.choices = [MagicMock()]
        mock_resp_1.choices[0].message.tool_calls = [tool_call_obj]
        mock_resp_1.choices[0].message.content = None
        mock_resp_1.choices[0].message.to_dict.return_value = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_abc123",
                    "type": "function",
                    "function": {"name": "create_invoice", "arguments": '{"customer_name": "Sarah Connor", "amount": 450.00, "description": "Velvet Drapes"}'},
                }
            ],
        }
        mock_resp_1.usage.prompt_tokens = 150
        mock_resp_1.usage.completion_tokens = 30

        mock_resp_2 = MagicMock()
        mock_resp_2.choices = [MagicMock()]
        mock_resp_2.choices[0].message.tool_calls = None
        mock_resp_2.choices[0].message.content = "Draft invoice generated for Sarah Connor in AED. Saved as draft — not sent."
        mock_resp_2.choices[0].message.to_dict.return_value = {
            "role": "assistant",
            "content": "Draft invoice generated for Sarah Connor in AED. Saved as draft — not sent.",
        }
        mock_resp_2.usage.prompt_tokens = 220
        mock_resp_2.usage.completion_tokens = 45

        with patch("litellm.acompletion", side_effect=[mock_resp_1, mock_resp_2]):
            result = await agent.process_user_turn(
                message_sid="SM_stage3_turn_1",
                from_number="+971501234567",
                user_message="Please create a draft invoice for Sarah Connor for 450 AED",
                reply_skill=mock_reply_skill,
            )

        self.assertEqual(result["status"], "completed")
        self.assertIn("Sarah Connor", result["reply"])
        mock_reply_skill.send_reply.assert_called_once()

        # Verify invoice draft was persisted to SQLite during the tool call
        with self.db_manager._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM invoices WHERE client_name = ?;", ("Sarah Connor",))
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["currency"], "AED")
            self.assertEqual(row["status"], "draft")

        # Verify token metering debited to SQLite
        spend = await self.db_manager.get_current_month_cost()
        self.assertGreater(spend, 0.0)

        # Verify conversation history updated
        history = await self.db_manager.get_recent_history(limit=5)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[1]["role"], "assistant")


if __name__ == "__main__":
    unittest.main()