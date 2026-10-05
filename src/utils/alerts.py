import argparse
import asyncio
from email.message import EmailMessage
import logging
import os
import smtplib
from typing import Any, Dict, Optional

from src.utils.security import mask_phone_number

logger = logging.getLogger("platform_alerts")


class EmailAlertChannel:
    """Sends platform alerts by email over SMTP (e.g. Gmail with an app password).

    All settings come from the environment so no address or password lives in code:
    ALERT_EMAIL (recipient), SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, ALERT_FROM_EMAIL.
    """

    def __init__(
        self,
        to_address: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        from_address: Optional[str] = None,
    ):
        self.to_address = (to_address or os.getenv("ALERT_EMAIL", "")).strip()
        self.host = (host or os.getenv("SMTP_HOST", "smtp.gmail.com")).strip()
        self.port = int(port or os.getenv("SMTP_PORT", "587"))
        self.username = (username or os.getenv("SMTP_USER", "")).strip()
        self.password = (password or os.getenv("SMTP_PASSWORD", "")).strip()
        self.from_address = (from_address or os.getenv("ALERT_FROM_EMAIL", "") or self.username).strip()

    def is_configured(self) -> bool:
        return bool(self.to_address and self.host and self.username and self.password and self.from_address)

    def _send_sync(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.from_address
        msg["To"] = self.to_address
        msg.set_content(body)

        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=15) as smtp:
                smtp.login(self.username, self.password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                smtp.starttls()
                smtp.login(self.username, self.password)
                smtp.send_message(msg)

    async def send(self, subject: str, body: str) -> bool:
        if not self.is_configured():
            logger.warning("[EMAIL ALERT SKIPPED] ALERT_EMAIL / SMTP_USER / SMTP_PASSWORD not configured.")
            return False
        try:
            await asyncio.to_thread(self._send_sync, subject, body)
            logger.info("[EMAIL ALERT SENT] Platform alert emailed.")
            return True
        except Exception as e:
            logger.error(f"[EMAIL ALERT FAILED] Could not send alert email: {e}")
            return False


async def send_platform_alert(
    subject: str,
    body: str,
    reply_client: Optional[Any] = None,
    email_channel: Optional[EmailAlertChannel] = None,
) -> Dict[str, bool]:
    """Delivers an alert to the platform operator on every configured channel.

    Email (ALERT_EMAIL) is the primary channel; WhatsApp (PLATFORM_ALERT_WHATSAPP) is used
    in addition when a reply client is supplied and the number is set.
    Returns which channels were attempted and whether each succeeded.
    """
    results: Dict[str, bool] = {}

    channel = email_channel or EmailAlertChannel()
    if channel.is_configured():
        results["email"] = await channel.send(subject, body)

    platform_phone = os.getenv("PLATFORM_ALERT_WHATSAPP", "").strip()
    if reply_client is not None and platform_phone:
        try:
            send_result = await reply_client.send_reply(to_number=platform_phone, message=f"{subject}\n{body}")
            results["whatsapp"] = bool(isinstance(send_result, dict) and send_result.get("success") is True)
        except Exception as e:
            logger.error(f"[WHATSAPP ALERT FAILED] Could not notify {mask_phone_number(platform_phone)}: {e}")
            results["whatsapp"] = False

    if not results:
        logger.warning(f"[PLATFORM ALERT UNDELIVERED] No alert channel configured. Alert was: {subject}")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a platform alert email to ALERT_EMAIL.")
    parser.add_argument("--test", action="store_true", help="Send a test alert to confirm email settings work.")
    parser.add_argument("subject", nargs="?", default="")
    parser.add_argument("body", nargs="?", default="")
    args = parser.parse_args()

    if args.test:
        subject = "[Assistant] Test alert"
        body = "This is a test alert from your WhatsApp business assistant. Email alerts are working."
    elif args.subject:
        subject, body = args.subject, args.body or args.subject
    else:
        parser.error("Provide --test or a subject.")

    channel = EmailAlertChannel()
    if not channel.is_configured():
        raise SystemExit("Email alerts are not configured: set ALERT_EMAIL, SMTP_USER and SMTP_PASSWORD in .env")
    ok = asyncio.run(channel.send(subject, body))
    if not ok:
        raise SystemExit("Alert email could not be sent. Check SMTP settings in .env and the logs above.")
    print("Alert email sent.")


if __name__ == "__main__":
    main()
