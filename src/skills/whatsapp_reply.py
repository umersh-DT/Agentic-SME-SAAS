import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional
import httpx

logger = logging.getLogger("whatsapp_reply_skill")


def split_message_text(text: str, max_chars: int = 4096) -> List[str]:
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


# Meta allows up to 4096 characters in a text message body.
META_TEXT_LIMIT = 4096
DEFAULT_GRAPH_API_VERSION = "v23.0"


def _whatsapp_recipient(phone: str) -> str:
    """Graph API recipients are digits in international format without '+'."""
    return "".join(ch for ch in (phone or "") if ch.isdigit())


class WhatsAppReplySkill:
    """Sends WhatsApp messages via Meta's WhatsApp Cloud API with retries, pacing, and chunking.

    Settings: WHATSAPP_TOKEN (bearer token), WHATSAPP_PHONE_NUMBER_ID, optional WHATSAPP_GRAPH_API_VERSION.
    """

    def __init__(
        self,
        access_token: Optional[str] = None,
        phone_number_id: Optional[str] = None,
        api_version: Optional[str] = None,
        max_chars: Optional[int] = None,
    ):
        self.access_token = (access_token or os.getenv("WHATSAPP_TOKEN", "")).strip()
        self.phone_number_id = (phone_number_id or os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")).strip()
        self.api_version = (
            api_version or os.getenv("WHATSAPP_GRAPH_API_VERSION", "") or DEFAULT_GRAPH_API_VERSION
        ).strip()
        self.max_chars = min(int(max_chars or META_TEXT_LIMIT), META_TEXT_LIMIT)

    @property
    def _base_url(self) -> str:
        return f"https://graph.facebook.com/{self.api_version}"

    @property
    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def _credentials_missing(self) -> bool:
        return not self.access_token or not self.phone_number_id

    async def send_reply(self, to_number: str, message: str) -> Dict[str, Any]:
        """Alias for send_message matching dispatcher and agent loop interface."""
        return await self.send_message(to_number=to_number, body=message)

    async def _post_with_retries(
        self,
        client: httpx.AsyncClient,
        url: str,
        max_retries: int,
        backoff_factor: float,
        **request_kwargs: Any,
    ) -> Dict[str, Any]:
        """POSTs with retries on 429/5xx and network errors. Returns {"ok": bool, "data"|"error": ...}."""
        attempt = 0
        last_error_text = ""
        while attempt < max_retries:
            attempt += 1
            try:
                resp = await client.post(url, headers=self._auth_headers, **request_kwargs)
                if resp.status_code in (200, 201):
                    return {"ok": True, "data": resp.json()}
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_error_text = f"HTTP {resp.status_code}: {resp.text}"
                    logger.warning(
                        f"[WHATSAPP RETRY] Transient error {resp.status_code} on attempt {attempt}/{max_retries}."
                    )
                else:
                    logger.error(f"[WHATSAPP API ERROR] Fatal status {resp.status_code}: {resp.text}")
                    return {
                        "ok": False,
                        "error": f"WhatsApp API fatal error {resp.status_code}",
                        "details": resp.text,
                    }
            except httpx.RequestError as e:
                last_error_text = str(e)
                logger.warning(f"[WHATSAPP NETWORK RETRY] {e} on attempt {attempt}/{max_retries}.")
            except Exception as e:
                logger.error(f"[WHATSAPP DISPATCH FAILED] Unexpected error: {e}")
                return {"ok": False, "error": str(e)}
            if attempt < max_retries:
                await asyncio.sleep(backoff_factor * (2 ** (attempt - 1)))
        return {"ok": False, "error": f"Failed after {max_retries} attempts: {last_error_text}"}

    async def send_message(
        self,
        to_number: str,
        body: str,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
    ) -> Dict[str, Any]:
        """Sends a text message (split into chunks if needed) with 429/5xx retry backoff and pacing."""
        if self._credentials_missing():
            logger.warning("WhatsApp credentials missing: WHATSAPP_TOKEN or WHATSAPP_PHONE_NUMBER_ID not configured.")
            return {"success": False, "error": "WHATSAPP_CREDENTIALS_MISSING", "message": body}

        recipient = _whatsapp_recipient(to_number)
        chunks = split_message_text(body, max_chars=self.max_chars)
        url = f"{self._base_url}/{self.phone_number_id}/messages"
        sent_ids: List[str] = []

        async with httpx.AsyncClient(timeout=10.0) as client:
            for idx, chunk in enumerate(chunks):
                # 300ms pacing delay between multi-part chunks
                if idx > 0:
                    await asyncio.sleep(0.3)
                payload = {
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": recipient,
                    "type": "text",
                    "text": {"preview_url": False, "body": chunk},
                }
                result = await self._post_with_retries(
                    client, url, max_retries, backoff_factor, json=payload
                )
                if not result["ok"]:
                    return {"success": False, "error": result["error"], "details": result.get("details"),
                            "sent_ids": sent_ids}
                message_id = ((result["data"].get("messages") or [{}])[0]).get("id", "")
                sent_ids.append(message_id)

        return {
            "success": True,
            "message_sid": sent_ids[-1] if sent_ids else "",
            "to": recipient,
            "chunks_sent": sent_ids,
            "total_chunks": len(chunks),
        }

    async def download_media(self, media_id: str) -> Dict[str, Any]:
        """Downloads inbound media (e.g. a voice note) by its media id.

        Returns {"success": True, "content": bytes, "mime_type": str} or {"success": False, "error": ...}.
        """
        if self._credentials_missing():
            return {"success": False, "error": "WHATSAPP_CREDENTIALS_MISSING"}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                meta = await client.get(f"{self._base_url}/{media_id}", headers=self._auth_headers)
                if meta.status_code != 200:
                    return {"success": False, "error": f"Media lookup failed {meta.status_code}", "details": meta.text}
                info = meta.json()
                media = await client.get(info["url"], headers=self._auth_headers)
                if media.status_code != 200:
                    return {"success": False, "error": f"Media download failed {media.status_code}"}
                return {
                    "success": True,
                    "content": media.content,
                    "mime_type": info.get("mime_type") or media.headers.get("content-type", ""),
                }
        except Exception as e:
            logger.error(f"[WHATSAPP MEDIA DOWNLOAD FAILED] {e}")
            return {"success": False, "error": str(e)}

    async def upload_media(self, content: bytes, mime_type: str, filename: str) -> Dict[str, Any]:
        """Uploads a file (e.g. a PDF invoice) and returns {"success": True, "media_id": ...}."""
        if self._credentials_missing():
            return {"success": False, "error": "WHATSAPP_CREDENTIALS_MISSING"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            result = await self._post_with_retries(
                client,
                f"{self._base_url}/{self.phone_number_id}/media",
                max_retries=3,
                backoff_factor=1.0,
                data={"messaging_product": "whatsapp", "type": mime_type},
                files={"file": (filename, content, mime_type)},
            )
        if not result["ok"]:
            return {"success": False, "error": result["error"]}
        return {"success": True, "media_id": result["data"].get("id", "")}

    async def send_document(
        self, to_number: str, media_id: str, filename: str, caption: str = ""
    ) -> Dict[str, Any]:
        """Sends a previously uploaded document (e.g. a PDF invoice) to a WhatsApp user."""
        if self._credentials_missing():
            return {"success": False, "error": "WHATSAPP_CREDENTIALS_MISSING"}
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": _whatsapp_recipient(to_number),
            "type": "document",
            "document": {"id": media_id, "filename": filename, "caption": caption},
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            result = await self._post_with_retries(
                client, f"{self._base_url}/{self.phone_number_id}/messages", 3, 1.0, json=payload
            )
        if not result["ok"]:
            return {"success": False, "error": result["error"]}
        return {"success": True, "message_sid": ((result["data"].get("messages") or [{}])[0]).get("id", "")}
