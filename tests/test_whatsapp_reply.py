import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from src.skills.whatsapp_reply import WhatsAppReplySkill


class TestWhatsAppReplySkill(unittest.IsolatedAsyncioTestCase):
    async def test_number_normalization(self):
        skill = WhatsAppReplySkill(account_sid="DUMMY_SID", auth_token="DUMMY_TOKEN")
        self.assertEqual(skill._normalize_whatsapp_number("+971501234567"), "whatsapp:+971501234567")
        self.assertEqual(skill._normalize_whatsapp_number("whatsapp:+971501234567"), "whatsapp:+971501234567")

    async def test_successful_transmission(self):
        skill = WhatsAppReplySkill(
            account_sid="MOCK_ACCOUNT_SID_TEST",
            auth_token="MOCK_AUTH_SECRET_TOKEN",
            default_sender="+14155238886",
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {
            "sid": "SM_mock_outbound_998877",
            "status": "queued",
        }

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            result = await skill.send_message(
                to_number="+971501234567",
                body="Hello, your quote is 500 AED.",
            )
            self.assertTrue(result["success"])
            self.assertEqual(result["message_sid"], "SM_mock_outbound_998877")
            self.assertEqual(result["to"], "whatsapp:+971501234567")
            self.assertEqual(result["from"], "whatsapp:+14155238886")

    async def test_missing_credentials(self):
        skill = WhatsAppReplySkill(account_sid="", auth_token="")
        result = await skill.send_message(to_number="+971501234567", body="Test message")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "TWILIO_CREDENTIALS_MISSING")


if __name__ == "__main__":
    unittest.main()