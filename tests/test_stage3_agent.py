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

    async def test_consecutive_invoices_increment_numbers_and_persist_both(self):
        """Generates two invoices and verifies unique numbers and 2 distinct rows in SQLite."""
        schema, callables = get_scoped_tools(tenant_id=self.tenant_id, base_data_dir=self.base_data_dir)
        create_invoice_fn = callables["create_invoice"]

        # 1. Create first invoice (Ali)
        res1 = await create_invoice_fn(
            customer_name="Ali Al-Maktoum",
            amount=2500.00,
            description="Custom Motorized Blackout Curtains",
            deposit_percentage=50.0,
            client_contact="+971509988776",
        )
        self.assertIn("AED", res1)
        self.assertIn("Draft invoice — not sent to client", res1)

        # 2. Create second invoice (Sara)
        res2 = await create_invoice_fn(
            customer_name="Sara Connor",
            amount=1800.00,
            description="Sheer Drapes Installation",
            deposit_percentage=25.0,
            client_contact="+971501234567",
        )
        self.assertIn("AED", res2)
        self.assertIn("Draft invoice — not sent to client", res2)

        with self.db_manager._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT invoice_number, client_name, grand_total, status FROM invoices ORDER BY invoice_number ASC;")
            rows = [dict(r) for r in cursor.fetchall()]

        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["invoice_number"], rows[1]["invoice_number"])
        self.assertEqual(rows[0]["client_name"], "Ali Al-Maktoum")
        self.assertEqual(rows[1]["client_name"], "Sara Connor")
        self.assertTrue(rows[0]["invoice_number"].endswith("1001"))
        self.assertTrue(rows[1]["invoice_number"].endswith("1002"))

    async def test_quota_alert_sent_exactly_once_per_month(self):
        """Fix 1: Two over-quota turns in the same month trigger exactly one platform alert[cite: 9]."""
        agent = LiteLLMAgent(
            tenant_id=self.tenant_id,
            plan_tier="starter",  # $5.00 monthly cap
            business_name="Luxe Curtain Interiors",
            base_data_dir=self.base_data_dir,
        )

        # Pre-seed token_usage table with spend exceeding the $5.00 starter cap ($5.20 spent)
        with self.db_manager._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO token_usage (message_sid, model, prompt_tokens, completion_tokens, total_tokens, cost_usd)
                VALUES ('SM_seed_spend', 'openai/gpt-4o-mini', 10000, 5000, 15000, 5.20);
                """
            )
            conn.commit()

        mock_reply_skill = MagicMock(spec=WhatsAppReplySkill)
        mock_reply_skill.send_reply = AsyncMock(
            return_value={"success": True, "message_sid": "SM_alert_test"}
        )

        with patch.dict("os.environ", {"PLATFORM_ALERT_WHATSAPP": "+971509990000"}):
            # Turn 1: Quota exceeded -> should send user cap notice AND platform alert
            res1 = await agent.process_user_turn(
                message_sid="SM_over_quota_turn_1",
                from_number="+971501234567",
                user_message="Hello assistant",
                reply_skill=mock_reply_skill,
            )
            self.assertEqual(res1["status"], "quota_exceeded")
            
            # send_reply called twice: once to user (+971501234567), once to platform (+971509990000)
            self.assertEqual(mock_reply_skill.send_reply.call_count, 2)
            called_recipients = [call.kwargs.get("to_number") for call in mock_reply_skill.send_reply.call_args_list]
            self.assertIn("+971509990000", called_recipients)

            # Turn 2: Second over-quota turn in same month -> should notify user but NOT alert platform again
            mock_reply_skill.send_reply.reset_mock()
            res2 = await agent.process_user_turn(
                message_sid="SM_over_quota_turn_2",
                from_number="+971501234567",
                user_message="Hello again",
                reply_skill=mock_reply_skill,
            )
            self.assertEqual(res2["status"], "quota_exceeded")
            
            # send_reply called only ONCE (to the user), platform alert was skipped
            self.assertEqual(mock_reply_skill.send_reply.call_count, 1)
            self.assertEqual(mock_reply_skill.send_reply.call_args.kwargs.get("to_number"), "+971501234567")

            # Verify platform_alerts table has exactly 1 row
            with self.db_manager._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT alert_type, period FROM platform_alerts WHERE alert_type = 'quota';")
                rows = cursor.fetchall()
                self.assertEqual(len(rows), 1)

    async def test_agent_turn_with_invoice_tool_and_token_metering(self):
        """Simulates full LLM tool turn with real WhatsAppReplySkill return shape."""
        agent = LiteLLMAgent(
            tenant_id=self.tenant_id,
            plan_tier="pro",
            business_name="Luxe Curtain Interiors",
            base_data_dir=self.base_data_dir,
        )

        mock_reply_skill = MagicMock(spec=WhatsAppReplySkill)
        mock_reply_skill.send_reply = AsyncMock(
            return_value={"success": True, "message_sid": "SM_real_sid_123"}
        )

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

        with self.db_manager._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM inbound_messages WHERE message_sid = ?;", ("SM_stage3_turn_1",))
            row = cursor.fetchone()
            self.assertEqual(row["status"], "completed")

    async def test_agent_turn_failed_send_marks_message_failed(self):
        """Verifies that when WhatsApp send fails, message status is 'failed'."""
        agent = LiteLLMAgent(
            tenant_id=self.tenant_id,
            plan_tier="pro",
            business_name="Luxe Curtain Interiors",
            base_data_dir=self.base_data_dir,
        )

        mock_reply_skill = MagicMock(spec=WhatsAppReplySkill)
        mock_reply_skill.send_reply = AsyncMock(
            return_value={"success": False, "error": "Twilio fatal 400"}
        )

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.tool_calls = None
        mock_resp.choices[0].message.content = "Here is your simple answer."
        mock_resp.usage.prompt_tokens = 50
        mock_resp.usage.completion_tokens = 10

        with patch("litellm.acompletion", return_value=mock_resp):
            result = await agent.process_user_turn(
                message_sid="SM_stage3_fail_send",
                from_number="+971501234567",
                user_message="Hello",
                reply_skill=mock_reply_skill,
            )

        self.assertEqual(result["status"], "failed")
        with self.db_manager._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM inbound_messages WHERE message_sid = ?;", ("SM_stage3_fail_send",))
            row = cursor.fetchone()
            self.assertEqual(row["status"], "failed")

    async def test_sender_history_isolation(self):
        """Verifies conversation history is isolated per sender phone number."""
        sender_owner = "+971501111111"
        sender_staff = "+971502222222"

        await self.db_manager.append_history(
            message_sid="SM_owner_1",
            from_number=sender_owner,
            role="user",
            content="Owner private financial update: profit margin is 45%",
        )

        staff_history = await self.db_manager.get_recent_history(limit=10, from_number=sender_staff)
        self.assertEqual(len(staff_history), 0)

        owner_history = await self.db_manager.get_recent_history(limit=10, from_number=sender_owner)
        self.assertEqual(len(owner_history), 1)
        self.assertIn("Owner private", owner_history[0]["content"])

    async def test_tool_round_limit_reached_returns_honest_fallback(self):
        """Verifies honest reply when the 4-round tool cap is reached without text."""
        agent = LiteLLMAgent(
            tenant_id=self.tenant_id,
            plan_tier="pro",
            business_name="Luxe Curtain Interiors",
            base_data_dir=self.base_data_dir,
        )

        mock_reply_skill = MagicMock(spec=WhatsAppReplySkill)
        mock_reply_skill.send_reply = AsyncMock(
            return_value={"success": True, "message_sid": "SM_tool_cap"}
        )

        tool_call_obj = MagicMock()
        tool_call_obj.id = "call_loop"
        tool_call_obj.function.name = "search_business_memory"
        tool_call_obj.function.arguments = '{"query": "pricing"}'

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.tool_calls = [tool_call_obj]
        mock_resp.choices[0].message.content = None
        mock_resp.choices[0].message.to_dict.return_value = {
            "role": "assistant",
            "tool_calls": [{"id": "call_loop", "type": "function", "function": {"name": "search_business_memory", "arguments": '{"query": "pricing"}'}}],
        }
        mock_resp.usage.prompt_tokens = 30
        mock_resp.usage.completion_tokens = 10

        with patch("litellm.acompletion", return_value=mock_resp):
            result = await agent.process_user_turn(
                message_sid="SM_tool_limit_test",
                from_number="+971501234567",
                user_message="Find all policies endlessly",
                reply_skill=mock_reply_skill,
            )

        self.assertEqual(result["reply"], "I couldn't complete that request. Please try rephrasing.")

    async def test_auth_error_triggers_fallback_with_zero_token_billing(self):
        """Verifies auth/API errors trigger standard fallback and bill 0 tokens."""
        agent = LiteLLMAgent(
            tenant_id=self.tenant_id,
            plan_tier="starter",
            business_name="Luxe Curtain Interiors",
            base_data_dir=self.base_data_dir,
        )

        mock_reply_skill = MagicMock(spec=WhatsAppReplySkill)
        mock_reply_skill.send_reply = AsyncMock(
            return_value={"success": True, "message_sid": "SM_fallback"}
        )

        with patch("litellm.acompletion", side_effect=Exception("AuthenticationError: Missing api_key")):
            result = await agent.process_user_turn(
                message_sid="SM_auth_err_test",
                from_number="+971501234567",
                user_message="Hello",
                reply_skill=mock_reply_skill,
            )

        self.assertIn("having trouble processing that request", result["reply"])
        spend = await self.db_manager.get_current_month_cost()
        self.assertEqual(spend, 0.0)


if __name__ == "__main__":
    unittest.main()