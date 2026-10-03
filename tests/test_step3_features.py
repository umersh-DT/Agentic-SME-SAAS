import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from src.core.storage_models import TenantDatabaseManager
from src.gateway.dispatcher import HELP_TEXT, TenantWorkerDispatcher
from src.skills.memory_tree import TenantMemoryTree

OWNER = "+971501234567"
STAFF = "+971502222222"


def _llm_text(text: str):
    message = SimpleNamespace(content=text, tool_calls=None, to_dict=lambda: {"role": "assistant", "content": text})
    return SimpleNamespace(choices=[SimpleNamespace(message=message)],
                           usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))


def _llm_invoice_call(customer: str, amount: float, description: str):
    call = SimpleNamespace(id="call_1", function=SimpleNamespace(
        name="create_invoice",
        arguments=f'{{"customer_name": "{customer}", "amount": {amount}, "description": "{description}"}}',
    ))
    data = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "create_invoice", "arguments": call.function.arguments}}
    ]}
    message = SimpleNamespace(content=None, tool_calls=[call], to_dict=lambda: data)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)],
                           usage=SimpleNamespace(prompt_tokens=150, completion_tokens=30))


class Step3Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dispatcher = TenantWorkerDispatcher(data_root=self.temp_dir.name)
        self.tenant_id = "tenant_step3_01"
        self.db = TenantDatabaseManager(tenant_id=self.tenant_id, base_dir=self.temp_dir.name)
        skill = self.dispatcher.reply_skill
        self.sent_texts = []
        self.sent_docs = []
        self.uploads = []

        async def send_reply(to_number, message):
            self.sent_texts.append((to_number, message))
            return {"success": True, "message_sid": f"wamid.out{len(self.sent_texts)}"}

        async def upload_media(content, mime_type, filename):
            self.uploads.append((filename, content, mime_type))
            return {"success": True, "media_id": f"MEDIA_{len(self.uploads)}"}

        async def send_document(to_number, media_id, filename, caption=""):
            self.sent_docs.append({"to": to_number, "media_id": media_id, "filename": filename, "caption": caption})
            return {"success": True, "message_sid": "wamid.doc"}

        self.patches = [
            patch.object(skill, "send_reply", side_effect=send_reply),
            patch.object(skill, "upload_media", side_effect=upload_media),
            patch.object(skill, "send_document", side_effect=send_document),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        self.temp_dir.cleanup()

    async def say(self, sender: str, body: str, sid: str, **kw):
        return await self.dispatcher.process_incoming_message(
            tenant_id=self.tenant_id, from_number=sender, owner_phone=OWNER,
            business_name="Step3 Cleaning", body=body, message_sid=sid, **kw,
        )


class TestOwnerCommands(Step3Base):
    async def _teach(self, *rules):
        with patch("litellm.acompletion", AsyncMock(return_value=_llm_text("Got it."))):
            for i, rule in enumerate(rules):
                await self.say(OWNER, rule, f"SM_teach_{i}")

    async def test_list_change_forget_rules_without_ai(self):
        await self._teach("Remember: we charge 150 AED per cleaning visit",
                          "Remember: deposits are 50% upfront",
                          "From now on we never work on Fridays")
        memory = TenantMemoryTree(self.tenant_id, self.temp_dir.name)

        with patch("litellm.acompletion", AsyncMock()) as mock_llm:
            listed = await self.say(OWNER, "list rules", "SM_list")
            changed = await self.say(OWNER, "change rule 1: We charge 200 AED per cleaning visit", "SM_change")
            forgot = await self.say(OWNER, "forget rule 2", "SM_forget")
            listed_after = await self.say(OWNER, "show my rules", "SM_list2")
        mock_llm.assert_not_called()

        self.assertEqual(listed["command"], "list_rules")
        self.assertIn("1. We charge 150 AED per cleaning visit", listed["reply"])
        self.assertIn("2. Deposits are 50% upfront", listed["reply"])
        self.assertIn("3. From now on we never work on Fridays", listed["reply"])
        self.assertEqual(changed["reply"], "Changed rule 1: We charge 200 AED per cleaning visit")
        self.assertEqual(forgot["reply"], "Forgot rule 2: Deposits are 50% upfront")
        self.assertIn("1. We charge 200 AED per cleaning visit", listed_after["reply"])
        self.assertIn("2. From now on we never work on Fridays", listed_after["reply"])
        self.assertNotIn("Deposits", listed_after["reply"])

        self.assertEqual([h["content"] for h in await memory.search_memory("charge")],
                         ["We charge 200 AED per cleaning visit"])
        self.assertEqual(await memory.search_memory("deposits"), [])
        self.assertEqual(await self.db.get_message_status("SM_forget"), "completed")

    async def test_unknown_rule_number(self):
        with patch("litellm.acompletion", AsyncMock()) as mock_llm:
            res = await self.say(OWNER, "forget rule 9", "SM_forget9")
        mock_llm.assert_not_called()
        self.assertEqual(res["reply"], 'There\'s no rule 9. Send "list rules" to see the numbers.')

    async def test_staff_cannot_list_change_or_forget_rules(self):
        await self._teach("Remember: deposits are 50% upfront")
        memory = TenantMemoryTree(self.tenant_id, self.temp_dir.name)
        with patch("litellm.acompletion", AsyncMock()) as mock_llm:
            replies = [
                (await self.say(STAFF, text, f"SM_staff_{i}"))["reply"]
                for i, text in enumerate(["list rules", "change rule 1: deposits are 10%", "forget rule 1"])
            ]
        mock_llm.assert_not_called()
        self.assertEqual(replies, ["Only the business owner can change business rules."] * 3)
        self.assertEqual([r["content"] for r in await memory.list_nodes()], ["Deposits are 50% upfront"])

    async def test_help(self):
        with patch("litellm.acompletion", AsyncMock()) as mock_llm:
            res = await self.say(STAFF, "help", "SM_help")
        mock_llm.assert_not_called()
        self.assertEqual(res["reply"], HELP_TEXT)
        self.assertEqual(self.sent_texts[-1], (STAFF, HELP_TEXT))

    async def test_saved_rule_tells_ai_not_to_repeat_disclaimer(self):
        with patch("litellm.acompletion", AsyncMock(return_value=_llm_text("Noted."))) as mock_llm:
            res = await self.say(OWNER, "Remember: we charge 150 AED per cleaning visit", "SM_rule")
        prompt = mock_llm.call_args.kwargs["messages"][0]["content"]
        self.assertIn("already saved this message as a business rule", prompt)
        self.assertIn("do not mention this limitation unless the user asks", prompt)
        self.assertEqual(res["reply"], "Saved: We charge 150 AED per cleaning visit\n\nNoted.")


class TestInvoicePdfFlow(Step3Base):
    async def _create_invoice(self, sender: str, sid: str):
        responses = [_llm_invoice_call("Ali", 500, "Deep cleaning"), _llm_text("Draft created.")]
        with patch("litellm.acompletion", AsyncMock(side_effect=responses)):
            return await self.say(sender, "Invoice Ali 500 AED for deep cleaning", sid)

    async def test_owner_invoice_gets_draft_pdf_then_approval_sends_final_pdf(self):
        res = await self._create_invoice(OWNER, "SM_inv_owner")
        number = res["invoices_created"][0]
        self.assertTrue(number.endswith("-1001"))

        self.assertEqual(len(self.sent_docs), 1)
        draft_doc = self.sent_docs[0]
        self.assertEqual(draft_doc["to"], OWNER)
        self.assertEqual(draft_doc["filename"], f"{number}-DRAFT.pdf")
        self.assertIn('Reply "approve invoice 1001" to finalise', draft_doc["caption"])
        draft_pdf = self.uploads[0][1]
        self.assertTrue(draft_pdf.startswith(b"%PDF"))
        self.assertIn(b"DRAFT", draft_pdf)
        self.assertIn(b"AED 500.00", draft_pdf)
        self.assertIn(b"AED 476.19", draft_pdf)
        self.assertIn(b"AED 23.81", draft_pdf)

        with patch("litellm.acompletion", AsyncMock()) as mock_llm:
            approved = await self.say(OWNER, "approve invoice 1001", "SM_approve")
        mock_llm.assert_not_called()
        self.assertEqual(approved["reply"], f"Invoice {number} approved. The final PDF (without DRAFT) is attached.")

        invoice = await self.db.get_invoice(number)
        self.assertEqual(invoice["status"], "approved")
        self.assertIsNotNone(invoice["approved_at"])
        final_doc = self.sent_docs[1]
        self.assertEqual((final_doc["to"], final_doc["filename"]), (OWNER, f"{number}.pdf"))
        self.assertNotIn(b"DRAFT", self.uploads[1][1])

        again = await self.say(OWNER, "approve invoice 1001", "SM_approve_again")
        self.assertEqual(again["reply"], f"Invoice {number} is already approved.")

    async def test_staff_invoice_pdf_goes_to_owner_and_staff_cannot_approve(self):
        res = await self._create_invoice(STAFF, "SM_inv_staff")
        number = res["invoices_created"][0]
        self.assertEqual(self.sent_docs[0]["to"], OWNER)

        with patch("litellm.acompletion", AsyncMock()):
            denied = await self.say(STAFF, "approve invoice 1001", "SM_staff_approve")
        self.assertEqual(denied["reply"], "Only the business owner can approve invoices.")
        self.assertEqual((await self.db.get_invoice(number))["status"], "draft")

    async def test_staff_told_when_owner_pdf_cannot_be_delivered(self):
        async def failing_send_document(*args, **kwargs):
            return {"success": False, "error": "WhatsApp API fatal error 400"}

        with patch.object(self.dispatcher.reply_skill, "send_document", side_effect=failing_send_document):
            res = await self._create_invoice(STAFF, "SM_inv_fail")
        note_to, note = self.sent_texts[-1]
        self.assertEqual(note_to, STAFF)
        self.assertIn(f"couldn't send the PDF of {res['invoices_created'][0]} to the owner", note)

    async def test_unknown_invoice(self):
        res = await self.say(OWNER, "approve invoice 4242", "SM_approve_unknown")
        self.assertEqual(res["reply"], "I couldn't find invoice 4242.")


class TestVoiceNotes(Step3Base):
    def _gemini_response(self, text: str, prompt_tokens=1000, completion_tokens=20):
        return httpx.Response(200, request=httpx.Request("POST", "https://generativelanguage.googleapis.com"), json={
            "candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": {"promptTokenCount": prompt_tokens, "candidatesTokenCount": completion_tokens},
        })

    async def _voice(self, transcript_response, sid="wamid.voice_1"):
        await self.db.persist_inbound_message(message_sid=sid, from_number=OWNER, body="", num_media=1, media_id="MEDIA_9")
        download = AsyncMock(return_value={"success": True, "content": b"OggS-fake-audio", "mime_type": "audio/ogg; codecs=opus"})
        with patch.dict(os.environ, {"LLM_MODEL": "gemini/gemini-2.5-flash", "GEMINI_API_KEY": "AIza-test"}), \
             patch.object(self.dispatcher.reply_skill, "download_media", download), \
             patch("httpx.AsyncClient.post", AsyncMock(return_value=transcript_response)) as mock_http, \
             patch("litellm.acompletion", AsyncMock(return_value=_llm_text("Noted."))) as mock_llm:
            res = await self.dispatcher.process_voice_message(
                tenant_id=self.tenant_id, from_number=OWNER, media_id="MEDIA_9", message_sid=sid,
                business_name="Step3 Cleaning", owner_phone=OWNER,
            )
        return res, download, mock_http, mock_llm

    async def test_voice_note_is_transcribed_answered_and_costed(self):
        res, download, mock_http, mock_llm = await self._voice(
            self._gemini_response("Remember: we charge 150 AED per cleaning visit"))

        download.assert_awaited_once_with("MEDIA_9")
        url = mock_http.call_args.args[0]
        self.assertEqual(url, "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent")
        self.assertEqual(mock_http.call_args.kwargs["headers"], {"x-goog-api-key": "AIza-test"})
        inline = mock_http.call_args.kwargs["json"]["contents"][0]["parts"][0]["inline_data"]
        self.assertEqual(inline["mime_type"], "audio/ogg")

        self.assertEqual(res["status"], "completed")
        self.assertEqual(res["rules_extracted"], 1)
        to, reply = self.sent_texts[-1]
        self.assertEqual(to, OWNER)
        self.assertTrue(reply.startswith(
            'You said: "Remember: we charge 150 AED per cleaning visit"\n\nSaved: We charge 150 AED per cleaning visit'
        ), reply)
        mock_llm.assert_called_once()
        history = await self.db.get_recent_history(limit=10, from_number=OWNER)
        self.assertEqual(history[0], {"role": "user", "content": "Remember: we charge 150 AED per cleaning visit"})

        with self.db._get_connection() as conn:
            voice_row = conn.execute(
                "SELECT model, prompt_tokens, completion_tokens, cost_usd FROM token_usage WHERE model LIKE '%(voice)';"
            ).fetchone()
            body = conn.execute("SELECT body FROM inbound_messages WHERE message_sid = 'wamid.voice_1';").fetchone()
        # 1000 audio tokens at $1.00/M + 20 output tokens at $2.50/M (rates in config/default_settings.yaml)
        self.assertEqual((voice_row["model"], voice_row["prompt_tokens"], voice_row["completion_tokens"]),
                         ("gemini/gemini-2.5-flash (voice)", 1000, 20))
        self.assertAlmostEqual(voice_row["cost_usd"], 0.00105, places=9)
        self.assertEqual(body["body"], "Remember: we charge 150 AED per cleaning visit")

    async def test_failed_transcription_tells_user(self):
        error = httpx.Response(400, request=httpx.Request("POST", "https://x"), json={"error": "bad audio"})
        res, _, _, mock_llm = await self._voice(error, sid="wamid.voice_bad")
        mock_llm.assert_not_called()
        self.assertEqual(res["status"], "failed")
        self.assertEqual(self.sent_texts[-1],
                         (OWNER, "Sorry, I couldn't understand that voice note. Please try again or type your message."))
        self.assertEqual(await self.db.get_message_status("wamid.voice_bad"), "failed")

    async def test_replay_reprocesses_pending_voice_note(self):
        await self.db.persist_inbound_message(message_sid="wamid.voice_crash", from_number=OWNER, body="",
                                              num_media=1, media_id="MEDIA_77")
        with patch.object(self.dispatcher, "process_voice_message", AsyncMock(return_value={"status": "completed"})) as pv:
            await self.dispatcher.replay_pending_messages(self.tenant_id, owner_phone=OWNER)
        self.assertEqual(pv.call_args.kwargs["media_id"], "MEDIA_77")
        self.assertEqual(pv.call_args.kwargs["message_sid"], "wamid.voice_crash")


if __name__ == "__main__":
    unittest.main()
