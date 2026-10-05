import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.context_extractor import ContextExtractor
from src.core.storage_models import TenantDatabaseManager
from src.gateway.dispatcher import TenantWorkerDispatcher
from src.skills.memory_tree import TenantMemoryTree


class TestTenantWorkerDispatcher(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dispatcher = TenantWorkerDispatcher(data_root=self.temp_dir.name)
        self.tenant_id = "tenant_unit_test_01"
        self.owner_phone = "+971501234567"
        self.staff_phone = "+971502222222"
        self.db_mgr = TenantDatabaseManager(tenant_id=self.tenant_id, base_dir=self.temp_dir.name)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    def _text_response(self, text: str) -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.tool_calls = None
        mock_resp.choices[0].message.content = text
        mock_resp.choices[0].message.to_dict.return_value = {"role": "assistant", "content": text}
        mock_resp.usage.prompt_tokens = 45
        mock_resp.usage.completion_tokens = 15
        return mock_resp

    async def _memory_rules(self):
        memory = TenantMemoryTree(tenant_id=self.tenant_id, base_data_dir=self.temp_dir.name)
        await memory.initialize()
        return memory

    async def test_owner_rule_is_saved_confirmed_and_searchable(self):
        """Owner teaches a rule: it is stored, confirmed with 'Saved:', and found by memory search."""
        with patch("litellm.acompletion", return_value=self._text_response("Noted.")), \
             patch.object(self.dispatcher.reply_skill, "send_reply", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"success": True, "message_sid": "SM_mock_reply"}

            result = await self.dispatcher.process_incoming_message(
                tenant_id=self.tenant_id,
                from_number=self.owner_phone,
                owner_phone=self.owner_phone,
                body="Remember our standard cancellation policy is 24 hours in advance.",
                message_sid="SM_unit_test_sid",
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tenant_id"], self.tenant_id)
        self.assertEqual(result["message_sid"], "SM_unit_test_sid")
        self.assertEqual(result["rules_extracted"], 1)

        mock_send.assert_called_once()
        sent_text = mock_send.call_args.kwargs["message"]
        self.assertTrue(
            sent_text.startswith("Saved: Our standard cancellation policy is 24 hours in advance."), sent_text
        )

        memory = await self._memory_rules()
        hits = await memory.search_memory("cancellation policy")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["content"], "Our standard cancellation policy is 24 hours in advance.")

    async def test_saved_rule_reaches_ai_on_later_question(self):
        """A rule saved earlier is given to the AI as a verified fact when someone later asks about it."""
        with patch("litellm.acompletion", return_value=self._text_response("Noted.")), \
             patch.object(self.dispatcher.reply_skill, "send_reply", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"success": True, "message_sid": "SM_mock_reply"}
            await self.dispatcher.process_incoming_message(
                tenant_id=self.tenant_id,
                from_number=self.owner_phone,
                owner_phone=self.owner_phone,
                body="From now on we require a 50% deposit on all curtain orders.",
                message_sid="SM_rule_1",
            )

        with patch("litellm.acompletion", return_value=self._text_response("50% upfront.")) as mock_llm, \
             patch.object(self.dispatcher.reply_skill, "send_reply", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"success": True, "message_sid": "SM_mock_reply_2"}
            result = await self.dispatcher.process_incoming_message(
                tenant_id=self.tenant_id,
                from_number=self.staff_phone,
                owner_phone=self.owner_phone,
                body="What deposit do we take on curtain orders?",
                message_sid="SM_question_1",
            )

        self.assertEqual(result["rules_extracted"], 0)
        system_prompt = mock_llm.call_args.kwargs["messages"][0]["content"]
        self.assertIn("From now on we require a 50% deposit on all curtain orders.", system_prompt)
        self.assertIn("STAFF member", system_prompt)

    async def test_staff_cannot_save_rules(self):
        """Staff trying to teach a rule get the owner-only notice and nothing is stored."""
        with patch("litellm.acompletion", return_value=self._text_response("Okay.")), \
             patch.object(self.dispatcher.reply_skill, "send_reply", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"success": True, "message_sid": "SM_mock_reply"}
            result = await self.dispatcher.process_incoming_message(
                tenant_id=self.tenant_id,
                from_number=self.staff_phone,
                owner_phone=self.owner_phone,
                body="From now on we charge 200 AED for delivery.",
                message_sid="SM_staff_rule",
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["rules_extracted"], 0)
        sent_text = mock_send.call_args.kwargs["message"]
        self.assertTrue(sent_text.startswith("Only the business owner can change business rules."), sent_text)

        memory = await self._memory_rules()
        self.assertEqual(await memory.search_memory("delivery"), [])

    async def test_owner_question_and_invoice_request_are_not_saved_as_rules(self):
        """Questions and invoice commands from the owner never become rules."""
        with patch("litellm.acompletion", return_value=self._text_response("Sure.")), \
             patch.object(self.dispatcher.reply_skill, "send_reply", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"success": True, "message_sid": "SM_mock_reply"}
            for sid, body in [
                ("SM_q", "What is our deposit policy?"),
                ("SM_inv", "Invoice Ali 2,500 AED for curtains, deposit required"),
            ]:
                result = await self.dispatcher.process_incoming_message(
                    tenant_id=self.tenant_id,
                    from_number=self.owner_phone,
                    owner_phone=self.owner_phone,
                    body=body,
                    message_sid=sid,
                )
                self.assertEqual(result["rules_extracted"], 0)
                self.assertFalse(mock_send.call_args.kwargs["message"].startswith("Saved:"))

        memory = await self._memory_rules()
        self.assertEqual(await memory.search_memory("deposit"), [])

    async def test_reprocessing_same_message_does_not_duplicate_rule(self):
        """A crash-replayed message re-confirms the rule without storing it twice."""
        memory = await self._memory_rules()
        extractor = ContextExtractor(memory_tree=memory)
        text = "Remember: we never work on Fridays."
        first = await extractor.extract_and_store(text, source_message_id="SM_dup")
        second = await extractor.extract_and_store(text, source_message_id="SM_dup")

        self.assertEqual(first.id, second.id)
        self.assertEqual(len(await memory.search_memory("Fridays")), 1)

    async def test_early_failure_still_sends_fallback_reply(self):
        """If processing fails before the AI runs, the sender still gets the fallback reply."""
        with patch("src.gateway.dispatcher.TenantDatabaseManager", side_effect=RuntimeError("disk unavailable")), \
             patch.object(self.dispatcher.reply_skill, "send_reply", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"success": True, "message_sid": "SM_fallback"}
            result = await self.dispatcher.process_incoming_message(
                tenant_id=self.tenant_id,
                from_number=self.owner_phone,
                owner_phone=self.owner_phone,
                body="Hello",
                message_sid="SM_early_fail",
            )

        self.assertEqual(result["status"], "failed")
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args.kwargs["to_number"], self.owner_phone)
        self.assertIn("having trouble processing that request", mock_send.call_args.kwargs["message"])

    async def test_logs_mask_sender_phone_number(self):
        """Dispatcher logs never contain the sender's full phone number."""
        with patch("litellm.acompletion", return_value=self._text_response("Hi.")), \
             patch.object(self.dispatcher.reply_skill, "send_reply", new_callable=AsyncMock) as mock_send, \
             self.assertLogs("tenant_worker_dispatcher", level="INFO") as logs:
            mock_send.return_value = {"success": True, "message_sid": "SM_mock_reply"}
            await self.dispatcher.process_incoming_message(
                tenant_id=self.tenant_id,
                from_number=self.owner_phone,
                owner_phone=self.owner_phone,
                body="Hello",
                message_sid="SM_mask_check",
            )

        joined = "\n".join(logs.output)
        self.assertNotIn(self.owner_phone, joined)
        self.assertIn("+9715***67", joined)

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