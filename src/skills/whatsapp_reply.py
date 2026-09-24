import asyncio
import logging
import os
from typing import Any, Dict, Optional
import httpx

logger = logging.getLogger("whatsapp_reply")


class WhatsAppReplySkill:
    """Sends outbound WhatsApp messages via the Twilio REST API.

    Endpoint: POST https://api.twilio.com/2010-04-01/Accounts/{AccountSid}/Messages.json
    """

    def __init__(
        self,
        account_sid: Optional[str] = None,
        auth_token: Optional[str] = None,
        default_sender: Optional[str] = None,
        max_retries: int = 3,
        timeout_seconds: float = 10.0,
    ):
        self.account_sid = account_sid or os.getenv("TWILIO_ACCOUNT_SID", "").strip()
        self.auth_token = auth_token or os.getenv("TWILIO_AUTH_TOKEN", "").strip()
        self.default_sender = default_sender or os.getenv("TWILIO_WHATSAPP_NUMBER", "").strip()
        self.max_retries = max_retries
        self.timeout = timeout_seconds

    def _normalize_whatsapp_number(self, number: str) -> str:
        """Ensures phone number has the 'whatsapp:' prefix required by Twilio."""
        cleaned = number.strip()
        if not cleaned.startswith("whatsapp:"):
            return f"whatsapp:{cleaned}"
        return cleaned

    @property
    def api_url(self) -> str:
        return f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"

    async def send_message(
        self,
        to_number: str,
        body: str,
        from_number: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Sends an outbound WhatsApp text message with exponential backoff on retries."""
        if not self.account_sid or not self.auth_token:
            logger.error("Twilio credentials missing: TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not configured.")
            return {
                "success": False,
                "error": "TWILIO_CREDENTIALS_MISSING",
                "details": "Account SID or Auth Token is unset.",
            }

        sender = from_number or self.default_sender
        if not sender:
            logger.error("Outbound sender phone number missing.")
            return {
                "success": False,
                "error": "SENDER_NUMBER_MISSING",
                "details": "No 'from_number' provided and TWILIO_WHATSAPP_NUMBER is unset.",
            }

        normalized_to = self._normalize_whatsapp_number(to_number)
        normalized_from = self._normalize_whatsapp_number(sender)

        payload = {
            "To": normalized_to,
            "From": normalized_from,
            "Body": body,
        }

        auth = (self.account_sid, self.auth_token)
        delay = 1.0

        for attempt in range(1, self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(
                        self.api_url,
                        data=payload,
                        auth=auth,
                    )

                if response.status_code in (200, 201):
                    res_data = response.json()
                    sid = res_data.get("sid", "")
                    logger.info(
                        f"[OUTBOUND SUCCESS] WhatsApp sent to {normalized_to} | "
                        f"SID={sid} | Status={res_data.get('status')}"
                    )
                    return {
                        "success": True,
                        "message_sid": sid,
                        "status": res_data.get("status"),
                        "to": normalized_to,
                        "from": normalized_from,
                    }

                # Retry on rate limits (429) or transient gateway errors (5xx)
                if response.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    logger.warning(
                        f"[OUTBOUND RETRY] Twilio API HTTP {response.status_code}. "
                        f"Attempt {attempt}/{self.max_retries}. Retrying in {delay}s..."
                    )
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue

                error_data = (
                    response.json()
                    if response.headers.get("content-type", "").startswith("application/json")
                    else {"text": response.text}
                )
                logger.error(f"[OUTBOUND ERROR] Twilio HTTP {response.status_code}: {error_data}")
                return {
                    "success": False,
                    "http_status": response.status_code,
                    "error": error_data.get("message", "Twilio API transmission failure"),
                    "code": error_data.get("code"),
                }

            except (httpx.RequestError, httpx.TimeoutException) as exc:
                if attempt < self.max_retries:
                    logger.warning(
                        f"[OUTBOUND NETWORK ERROR] {type(exc).__name__}: {exc}. "
                        f"Attempt {attempt}/{self.max_retries}. Retrying in {delay}s..."
                    )
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    logger.error(f"[OUTBOUND FAILED] All {self.max_retries} attempts failed: {exc}")
                    return {
                        "success": False,
                        "error": "NETWORK_ERROR",
                        "details": str(exc),
                    }

        return {
            "success": False,
            "error": "MAX_RETRIES_EXCEEDED",
        }