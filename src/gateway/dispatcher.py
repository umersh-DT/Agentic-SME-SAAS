import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from src.core.agent_loop import LiteLLMAgent, resolve_model_name
from src.core.context_extractor import ContextExtractor
from src.core.storage_models import TenantDatabaseManager, load_default_settings
from src.skills.invoice_pdf import render_invoice_pdf
from src.skills.memory_tree import TenantMemoryTree
from src.skills.voice import transcribe_audio
from src.skills.whatsapp_reply import WhatsAppReplySkill
from src.utils.security import mask_phone_number, normalize_phone_number

logger = logging.getLogger("tenant_worker_dispatcher")


OWNER_ONLY_RULES_NOTICE = "Only the business owner can change business rules."
OWNER_ONLY_APPROVE_NOTICE = "Only the business owner can approve invoices."
RULE_SAVE_FAILED_NOTICE = "Sorry, I couldn't save that rule. Please try again."
VOICE_FAILED_REPLY = "Sorry, I couldn't understand that voice note. Please try again or type your message."
VOICE_EMPTY_REPLY = "I couldn't hear any words in that voice note. Please try again or type your message."
VOICE_TOO_LONG_REPLY = "That voice note is too long for me. Please send a shorter one or type your message."

HELP_TEXT = (
    "Here's what I can do:\n"
    "• Answer questions using your saved business rules\n"
    "• Draft invoices, e.g. \"Invoice Ali 500 AED for deep cleaning\" (the amount includes 5% VAT)\n"
    "• Understand voice notes — just speak your request\n\n"
    "Owner only:\n"
    "• Teach a rule: \"Remember: we charge 150 AED per visit\"\n"
    "• \"list rules\" · \"change rule 2: new text\" · \"forget rule 2\"\n"
    "• \"approve invoice 1001\" to finalise a draft invoice"
)

# Owner commands handled directly (no AI call, no cost).
HELP_RE = re.compile(r"^\s*(?:help|menu|commands)\s*[?.!]*\s*$", re.IGNORECASE)
LIST_RULES_RE = re.compile(
    r"^\s*(?:(?:list|show|see|view)\s+(?:me\s+)?(?:my\s+|all\s+|the\s+|our\s+)?(?:business\s+)?rules|(?:my\s+)?rules)"
    r"\s*[?.!]*\s*$",
    re.IGNORECASE,
)
FORGET_RULE_RE = re.compile(r"^\s*(?:forget|delete|remove)\s+rule\s*#?\s*(\d+)\s*[.!]*\s*$", re.IGNORECASE)
CHANGE_RULE_RE = re.compile(
    r"^\s*(?:change|update|edit)\s+rule\s*#?\s*(\d+)\s*(?:to\b|:|-|=)?\s*(.+?)\s*$", re.IGNORECASE | re.DOTALL
)
APPROVE_INVOICE_RE = re.compile(r"^\s*approve\s+invoice\s*#?\s*([A-Za-z0-9-]+)\s*[.!]*\s*$", re.IGNORECASE)


class TenantWorkerDispatcher:
    """Dispatches inbound messages to tenant worker threads with crash durability and pre-flight deduplication."""

    def __init__(self, data_root: str = "/app/data/tenants"):
        self.data_root = os.environ.get("TENANTS_DATA_DIR", data_root)
        self.settings = load_default_settings()
        self.reply_skill = WhatsAppReplySkill(
            max_chars=self.settings.get("whatsapp", {}).get("max_message_chars")
        )

    # ------------------------------------------------------------------ rules

    async def _memory(self, tenant_id: str) -> TenantMemoryTree:
        memory = TenantMemoryTree(tenant_id=tenant_id, base_data_dir=self.data_root)
        await memory.initialize()
        return memory

    async def _handle_business_rule(
        self, tenant_id: str, body: str, message_sid: str, is_owner: bool
    ) -> Tuple[Optional[str], int]:
        """Saves a rule the owner teaches. Returns (notice for the reply, number of rules saved)."""
        if not ContextExtractor.looks_like_rule(body):
            return None, 0

        if not is_owner:
            logger.info(f"[RULES] Tenant={tenant_id} non-owner tried to set a rule (message_sid={message_sid}).")
            return OWNER_ONLY_RULES_NOTICE, 0

        try:
            memory = await self._memory(tenant_id)
            node = await ContextExtractor(memory_tree=memory).extract_and_store(
                text=body, source_message_id=message_sid
            )
        except Exception as e:
            logger.error(f"[RULES] Tenant={tenant_id} failed to save rule: {e}", exc_info=True)
            return RULE_SAVE_FAILED_NOTICE, 0

        if not node:
            return None, 0
        logger.info(f"[RULES] Tenant={tenant_id} saved rule {node.id} from message_sid={message_sid}.")
        return f"Saved: {node.content}", 1

    async def _rule_by_number(self, memory: TenantMemoryTree, number: int) -> Optional[Dict[str, Any]]:
        rules = await memory.list_nodes()
        return rules[number - 1] if 1 <= number <= len(rules) else None

    # --------------------------------------------------------------- commands

    async def _handle_command(
        self, tenant_id: str, business_name: str, body: str, is_owner: bool, owner_phone: Optional[str]
    ) -> Optional[Tuple[str, str]]:
        """Handles help / rule management / invoice approval. Returns (command name, reply) or None."""
        if HELP_RE.match(body):
            return "help", HELP_TEXT

        list_match = LIST_RULES_RE.match(body)
        forget_match = FORGET_RULE_RE.match(body)
        change_match = CHANGE_RULE_RE.match(body)
        approve_match = APPROVE_INVOICE_RE.match(body)

        if (list_match or forget_match or change_match) and not is_owner:
            return "rules_denied", OWNER_ONLY_RULES_NOTICE
        if approve_match and not is_owner:
            return "approve_denied", OWNER_ONLY_APPROVE_NOTICE

        if list_match:
            rules = await (await self._memory(tenant_id)).list_nodes()
            if not rules:
                return "list_rules", "No business rules saved yet. Teach one like: \"Remember: we charge 150 AED per visit\""
            lines = [f"{i}. {rule['content']}" for i, rule in enumerate(rules, start=1)]
            return "list_rules", (
                "Your business rules:\n" + "\n".join(lines)
                + "\n\nTo edit: \"change rule 2: new text\" · To delete: \"forget rule 2\""
            )

        if forget_match:
            number = int(forget_match.group(1))
            memory = await self._memory(tenant_id)
            rule = await self._rule_by_number(memory, number)
            if not rule:
                return "forget_rule", f"There's no rule {number}. Send \"list rules\" to see the numbers."
            await memory.delete_node(rule["id"])
            logger.info(f"[RULES] Tenant={tenant_id} owner deleted rule {rule['id']}.")
            return "forget_rule", f"Forgot rule {number}: {rule['content']}"

        if change_match:
            number, new_text = int(change_match.group(1)), change_match.group(2)
            memory = await self._memory(tenant_id)
            rule = await self._rule_by_number(memory, number)
            if not rule:
                return "change_rule", f"There's no rule {number}. Send \"list rules\" to see the numbers."
            update = ContextExtractor.normalize_rule(new_text)
            await memory.update_node_content(rule["id"], update.content, update.title, update.concepts)
            logger.info(f"[RULES] Tenant={tenant_id} owner changed rule {rule['id']}.")
            return "change_rule", f"Changed rule {number}: {update.content}"

        if approve_match:
            return "approve_invoice", await self._approve_invoice(
                tenant_id, business_name, approve_match.group(1), owner_phone
            )

        return None

    # ---------------------------------------------------------------- invoices

    async def _send_invoice_pdf(
        self, invoice: Dict[str, Any], business_name: str, to_number: str, draft: bool
    ) -> Dict[str, Any]:
        pdf = await asyncio.to_thread(render_invoice_pdf, invoice, business_name, draft)
        number = invoice["invoice_number"]
        filename = f"{number}{'-DRAFT' if draft else ''}.pdf"
        upload = await self.reply_skill.upload_media(pdf, "application/pdf", filename)
        if not upload.get("success"):
            return upload
        sequence = number.rsplit("-", 1)[-1]
        total = f"{invoice.get('currency') or 'AED'} {float(invoice['grand_total']):,.2f}"
        caption = (
            f"Draft invoice {number} for {invoice['client_name']}: {total}. "
            f"Reply \"approve invoice {sequence}\" to finalise."
            if draft
            else f"Approved invoice {number} for {invoice['client_name']}: {total}."
        )
        return await self.reply_skill.send_document(to_number, upload["media_id"], filename, caption)

    async def _send_draft_pdfs_to_owner(
        self,
        tenant_id: str,
        business_name: str,
        owner_phone: Optional[str],
        invoice_numbers: List[str],
        from_number: str,
    ) -> None:
        """Sends each new draft invoice to the owner as a PDF; tells the sender if that fails."""
        if not invoice_numbers:
            return
        if not owner_phone:
            logger.warning(f"[INVOICE PDF] Tenant={tenant_id} has no owner_phone; PDF not sent.")
            return
        db_mgr = TenantDatabaseManager(tenant_id=tenant_id, base_dir=self.data_root)
        for number in invoice_numbers:
            invoice = await db_mgr.get_invoice(number)
            if not invoice:
                continue
            try:
                result = await self._send_invoice_pdf(invoice, business_name, owner_phone, draft=True)
            except Exception as e:
                result = {"success": False, "error": str(e)}
            if result.get("success"):
                logger.info(f"[INVOICE PDF] Tenant={tenant_id} sent draft {number} to owner.")
                continue
            logger.error(f"[INVOICE PDF] Tenant={tenant_id} could not send {number}: {result.get('error')}")
            note = (
                f"Note: I couldn't send the PDF of {number} to the owner. "
                "WhatsApp only lets me message the owner within 24 hours of their last message to me."
                if normalize_phone_number(owner_phone) != normalize_phone_number(from_number)
                else f"Note: I couldn't create the PDF of {number} right now. The draft is saved."
            )
            await self.reply_skill.send_reply(to_number=from_number, message=note)

    async def _approve_invoice(
        self, tenant_id: str, business_name: str, reference: str, owner_phone: Optional[str]
    ) -> str:
        db_mgr = TenantDatabaseManager(tenant_id=tenant_id, base_dir=self.data_root)
        invoice = await db_mgr.find_invoice(reference)
        if not invoice:
            return f"I couldn't find invoice {reference}."
        number = invoice["invoice_number"]
        if invoice.get("status") == "approved":
            return f"Invoice {number} is already approved."

        await db_mgr.approve_invoice(number)
        invoice = await db_mgr.get_invoice(number)
        logger.info(f"[INVOICE] Tenant={tenant_id} owner approved {number}.")
        try:
            result = await self._send_invoice_pdf(invoice, business_name, owner_phone, draft=False)
        except Exception as e:
            result = {"success": False, "error": str(e)}
        if result.get("success"):
            return f"Invoice {number} approved. The final PDF (without DRAFT) is attached."
        logger.error(f"[INVOICE PDF] Tenant={tenant_id} could not send approved {number}: {result.get('error')}")
        return f"Invoice {number} approved, but I couldn't send the final PDF right now."

    # ------------------------------------------------------------- main entry

    async def process_incoming_message(
        self,
        tenant_id: str,
        from_number: str,
        body: str,
        message_sid: str,
        plan_tier: str = "starter",
        business_name: str = "Business Assistant",
        owner_phone: Optional[str] = None,
        heard_prefix: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Processes an incoming WhatsApp message with crash durability checks.

        heard_prefix is placed first in the reply (e.g. 'You said: "..."' for voice notes).
        """
        logger.info(
            f"[DISPATCHER] Processing message_sid={message_sid} for tenant={tenant_id} "
            f"from {mask_phone_number(from_number)}"
        )
        db_mgr: Optional[TenantDatabaseManager] = None

        def _join(*parts: Optional[str]) -> Optional[str]:
            joined = "\n\n".join(p for p in parts if p)
            return joined or None

        try:
            db_mgr = TenantDatabaseManager(tenant_id=tenant_id, base_dir=self.data_root)

            current_status = await db_mgr.get_message_status(message_sid)
            if current_status in ("completed", "processing"):
                logger.warning(f"[DISPATCHER] Dropping message_sid={message_sid} (already {current_status}).")
                return {"status": "skipped", "reason": f"already_{current_status}", "message_sid": message_sid}

            is_owner = bool(owner_phone) and normalize_phone_number(from_number) == normalize_phone_number(
                owner_phone
            )

            # 1. Commands (help, rules, approvals) are answered directly without the AI
            command = await self._handle_command(tenant_id, business_name, body, is_owner, owner_phone)
            if command:
                name, reply = command
                reply = _join(heard_prefix, reply)
                await db_mgr.persist_inbound_message(message_sid=message_sid, from_number=from_number, body=body)
                await db_mgr.append_history(message_sid=message_sid, role="user", content=body, from_number=from_number)
                await db_mgr.append_history(
                    message_sid=message_sid, role="assistant", content=reply, from_number=from_number
                )
                send_result = await self.reply_skill.send_reply(to_number=from_number, message=reply)
                ok = isinstance(send_result, dict) and send_result.get("success") is True
                await db_mgr.update_message_status(message_sid, "completed" if ok else "failed")
                return {
                    "status": "completed" if ok else "failed",
                    "tenant_id": tenant_id,
                    "message_sid": message_sid,
                    "reply": reply,
                    "command": name,
                    "rules_extracted": 0,
                }

            # 2. Business rules: only the owner can teach them; saved before the AI turn so it can use them
            rule_notice, rules_saved = await self._handle_business_rule(
                tenant_id=tenant_id, body=body, message_sid=message_sid, is_owner=is_owner
            )
            system_note = None
            if rules_saved:
                system_note = (
                    f"The system has already saved this message as a business rule and told the user "
                    f"'{rule_notice}'. Just acknowledge briefly; do not repeat the rule or talk about saving."
                )
            elif rule_notice == OWNER_ONLY_RULES_NOTICE:
                system_note = (
                    "The sender tried to change a business rule but is not the owner; the system has already "
                    "told them only the owner can do that. Do not repeat it; help with anything else they asked."
                )

            # 3. AI turn
            agent = LiteLLMAgent(
                tenant_id=tenant_id,
                plan_tier=plan_tier,
                business_name=business_name,
                base_data_dir=self.data_root,
            )
            result = await agent.process_user_turn(
                message_sid=message_sid,
                from_number=from_number,
                user_message=body,
                reply_skill=self.reply_skill,
                is_owner=is_owner,
                reply_prefix=_join(heard_prefix, rule_notice),
                system_note=system_note,
            )

            # 4. New draft invoices go to the owner as PDFs
            await self._send_draft_pdfs_to_owner(
                tenant_id, business_name, owner_phone, result.get("invoices_created") or [], from_number
            )

            return {
                "status": result.get("status", "completed"),
                "tenant_id": tenant_id,
                "message_sid": message_sid,
                "reply": result.get("reply", ""),
                "rules_extracted": rules_saved,
                "invoices_created": result.get("invoices_created") or [],
            }
        except Exception as e:
            logger.error(
                f"[DISPATCHER CRASH] Processing failed for message_sid={message_sid}: {e}",
                exc_info=True,
            )
            fallback_msg = self.settings.get("whatsapp", {}).get(
                "error_fallback_reply",
                "I'm having trouble processing that request right now. Please try again in a moment.",
            )
            try:
                await self.reply_skill.send_reply(to_number=from_number, message=fallback_msg)
            except Exception as send_err:
                logger.error(f"[DISPATCHER FALLBACK FAILED] Could not send fallback reply: {send_err}")
            if db_mgr is not None:
                try:
                    await db_mgr.update_message_status(message_sid, "failed")
                except Exception as status_err:
                    logger.error(f"[DISPATCHER] Could not mark message_sid={message_sid} failed: {status_err}")
            return {
                "status": "failed",
                "tenant_id": tenant_id,
                "message_sid": message_sid,
                "reply": fallback_msg,
                "error": str(e),
            }

    async def process_voice_message(
        self,
        tenant_id: str,
        from_number: str,
        media_id: str,
        message_sid: str,
        plan_tier: str = "starter",
        business_name: str = "Business Assistant",
        owner_phone: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Downloads and transcribes a voice note, records its cost, then handles it like a typed message."""
        db_mgr = TenantDatabaseManager(tenant_id=tenant_id, base_dir=self.data_root)
        if await db_mgr.get_message_status(message_sid) in ("completed", "processing"):
            return {"status": "skipped", "message_sid": message_sid}

        async def _fail(reply: str, reason: str) -> Dict[str, Any]:
            await self.reply_skill.send_reply(to_number=from_number, message=reply)
            await db_mgr.update_message_status(message_sid, "failed")
            return {"status": "failed", "reason": reason, "message_sid": message_sid, "reply": reply}

        voice_cfg = self.settings.get("voice", {})
        try:
            media = await self.reply_skill.download_media(media_id)
            if not media.get("success"):
                logger.error(f"[VOICE] Tenant={tenant_id} download failed: {media.get('error')}")
                return await _fail(VOICE_FAILED_REPLY, "download_failed")
            if len(media["content"]) > int(voice_cfg.get("max_bytes", 10 * 1024 * 1024)):
                return await _fail(VOICE_TOO_LONG_REPLY, "too_long")

            transcript = await transcribe_audio(
                media["content"], media.get("mime_type", "audio/ogg"), resolve_model_name(self.settings), self.settings
            )
        except Exception as e:
            logger.error(f"[VOICE] Tenant={tenant_id} transcription failed: {e}", exc_info=True)
            return await _fail(VOICE_FAILED_REPLY, "transcription_failed")

        await db_mgr.record_usage_cost(
            message_sid=message_sid,
            model=f"{transcript['model']} (voice)",
            cost_usd=transcript["cost_usd"],
            prompt_tokens=transcript["prompt_tokens"],
            completion_tokens=transcript["completion_tokens"],
        )
        text = transcript["text"]
        if not text:
            return await _fail(VOICE_EMPTY_REPLY, "empty_transcript")

        await db_mgr.set_message_body(message_sid, text)
        logger.info(f"[VOICE] Tenant={tenant_id} transcribed message_sid={message_sid} ({len(text)} chars).")
        return await self.process_incoming_message(
            tenant_id=tenant_id,
            from_number=from_number,
            body=text,
            message_sid=message_sid,
            plan_tier=plan_tier,
            business_name=business_name,
            owner_phone=owner_phone,
            heard_prefix=f"You said: \"{text}\"",
        )

    async def replay_pending_messages(
        self,
        tenant_id: str,
        plan_tier: str = "starter",
        business_name: str = "Business Assistant",
        owner_phone: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Recovers unfinished messages after a restart. Resets crashed 'processing' rows to 'pending'."""
        db_mgr = TenantDatabaseManager(tenant_id=tenant_id, base_dir=self.data_root)

        def _reset_and_fetch_pending():
            with db_mgr._get_connection() as conn:
                # 1. Reset messages caught mid-turn when container stopped/crashed
                conn.execute(
                    """
                    UPDATE inbound_messages
                    SET status = 'pending', updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'processing';
                    """
                )
                conn.commit()

                # 2. Fetch all pending messages for re-execution
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT message_sid, from_number, body, num_media, media_id
                    FROM inbound_messages
                    WHERE status = 'pending'
                    ORDER BY created_at ASC;
                    """
                )
                return [dict(row) for row in cursor.fetchall()]

        pending_rows = await asyncio.to_thread(_reset_and_fetch_pending)
        replayed_results = []
        for msg in pending_rows:
            logger.info(
                f"[REPLAY] Replaying recovered pending message {msg['message_sid']} for tenant={tenant_id}"
            )
            if msg.get("media_id") and not msg.get("body"):
                res = await self.process_voice_message(
                    tenant_id=tenant_id,
                    from_number=msg["from_number"],
                    media_id=msg["media_id"],
                    message_sid=msg["message_sid"],
                    plan_tier=plan_tier,
                    business_name=business_name,
                    owner_phone=owner_phone,
                )
            else:
                res = await self.process_incoming_message(
                    tenant_id=tenant_id,
                    plan_tier=plan_tier,
                    business_name=business_name,
                    owner_phone=owner_phone,
                    from_number=msg["from_number"],
                    body=msg["body"] or "",
                    message_sid=msg["message_sid"],
                )
            replayed_results.append(res)
        return replayed_results
