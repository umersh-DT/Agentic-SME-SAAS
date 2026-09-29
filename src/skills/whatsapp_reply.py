import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional
import httpx

logger = logging.getLogger("whatsapp_reply_skill")


def split_message_text(text: str, max_chars: int = 1550) -> List[str]:
    """Splits text into chunks <= max_chars along paragraph or sentence boundaries."""
    if not text or len(text) <= max_chars:
        return [text] if text else []

    chunks: List[str] = []
    current_chunk = ""

    paragraphs = text.split("\n\n")

    for para in paragraphs:
        if len(para) > max_chars:
            sentences = re.split(r"(?<=[.!?])\s+", para)
            for s in sentences:
                if len(current_chunk) + len(s) + 1 <= max_chars:
                    current_chunk = f"{current_chunk} {s}".strip()
                else:
                    if current_chunk:
                        chunks.append(current_chunk)
                    if len(s) > max_chars:
                        for i in range(0, len(s), max_chars):
                            chunks.append(s[i : i + max_chars])
                        current_chunk = ""
                    else:
                        current_chunk = s
        else:
            if len(current_chunk) + len(para) + 2 <= max_chars:
                current_chunk = f"{current_chunk}\n\n{para}".strip()
            else:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = para

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


class WhatsAppReplySkill:
    """Dispatches outbound WhatsApp messages via Twilio REST API with retries, pacing, and chunking."""

    def __init__(
        self,
        account_sid: Optional[str] = None,
        auth_token: Optional[str] = None,
        from_number: Optional[str] = None,
        default_sender: Optional[str] = None,
    ):
        self.account_sid = (account_sid or os.getenv("TWILIO_ACCOUNT_SID", "")).strip()
        self.auth_token = (auth_token or os.getenv("TWILIO_AUTH_TOKEN", "")).strip()
        raw_sender = default_sender or from_number or os.getenv("TWILIO_WHATSAPP_NUMBER", "+14155238886")
        self.from_number = self._normalize_whatsapp_number(raw_sender)

    def _normalize_whatsapp_number(self, phone: str) -> str:
        if not phone:
            return ""
        cleaned = phone.strip()
        return cleaned if cleaned.startswith("whatsapp:") else f"whatsapp:{cleaned}"

    async def send_reply(self, to_number: str, message: str) -> Dict[str, Any]:
        """Alias for send_message matching dispatcher and agent loop interface."""
        return await self.send_message(to_number=to_number, body=message)

    async def send_message(
        self,
        to_number: str,
        body: str,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
    ) -> Dict[str, Any]:
        """Dispatches outbound WhatsApp message chunks with 429/5xx retry backoff and pacing."""
        if not self.account_sid or not self.auth_token:
            logger.warning("Twilio credentials missing: TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not configured.")
            return {
                "success": False,
                "error": "TWILIO_CREDENTIALS_MISSING",
                "message": body,
            }

        target_to = self._normalize_whatsapp_number(to_number)
        target_from = self.from_number
        chunks = split_message_text(body, max_chars=1550)

        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        auth = (self.account_sid, self.auth_token)
        last_sid = ""
        sent_sids: List[str] = []

        async with httpx.AsyncClient(timeout=10.0) as client:
            for idx, chunk in enumerate(chunks):
                # 300ms pacing delay between multi-part chunks
                if idx > 0:
                    await asyncio.sleep(0.3)

                data = {
                    "To": target_to,
                    "From": target_from,
                    "Body": chunk,
                }

                chunk_delivered = False
                attempt = 0
                last_error_text = ""

                while attempt < max_retries and not chunk_delivered:
                    attempt += 1
                    try:
                        resp = await client.post(url, data=data, auth=auth)
                        if resp.status_code in (200, 201):
                            resp_data = resp.json()
                            sid = resp_data.get("sid", f"SM_mock_{idx}")
                            last_sid = sid
                            sent_sids.append(sid)
                            chunk_delivered = True
                        elif resp.status_code in (429, 500, 502, 503, 504):
                            last_error_text = f"HTTP {resp.status_code}: {resp.text}"
                            logger.warning(
                                f"[TWILIO RETRY] Transient error {resp.status_code} on attempt {attempt}/{max_retries}. Backing off."
                            )
                            if attempt < max_retries:
                                await asyncio.sleep(backoff_factor * (2 ** (attempt - 1)))
                        else:
                            logger.error(f"[TWILIO REST ERROR] Fatal status {resp.status_code}: {resp.text}")
                            return {
                                "success": False,
                                "error": f"Twilio API fatal error {resp.status_code}",
                                "details": resp.text,
                            }
                    except httpx.RequestError as e:
                        last_error_text = str(e)
                        logger.warning(
                            f"[TWILIO NETWORK RETRY] Network error {e} on attempt {attempt}/{max_retries}."
                        )
                        if attempt < max_retries:
                            await asyncio.sleep(backoff_factor * (2 ** (attempt - 1)))
                    except Exception as e:
                        logger.error(f"[TWILIO DISPATCH FAILED] Unexpected error: {e}")
                        return {"success": False, "error": str(e)}

                if not chunk_delivered:
                    return {
                        "success": False,
                        "error": f"Failed after {max_retries} attempts: {last_error_text}",
                        "sent_sids": sent_sids,
                    }

        return {
            "success": True,
            "message_sid": last_sid,
            "to": target_to,
            "from": target_from,
            "chunks_sent": sent_sids,
            "total_chunks": len(chunks),
        }