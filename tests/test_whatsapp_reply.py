import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from src.skills.whatsapp_reply import WhatsAppReplySkill, split_message_text


def _response(status: int, payload=None, content: bytes = b"", headers=None) -> httpx.Response:
    request = httpx.Request("POST", "https://graph.facebook.com")
    if payload is not None:
        return httpx.Response(status, json=payload, request=request, headers=headers)
    return httpx.Response(status, content=content, request=request, headers=headers)


class TestWhatsAppReplySkill(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.skill = WhatsAppReplySkill(access_token="TEST_TOKEN", phone_number_id="109876543210", api_version="v23.0")

    async def test_successful_transmission_uses_graph_api(self):
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = _response(200, {"messages": [{"id": "wamid.outbound_998877"}]})
            result = await self.skill.send_message(to_number="+971501234567", body="Hello, your quote is 500 AED.")

        self.assertTrue(result["success"])
        self.assertEqual(result["message_sid"], "wamid.outbound_998877")
        self.assertEqual(result["to"], "971501234567")

        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://graph.facebook.com/v23.0/109876543210/messages")
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer TEST_TOKEN"})
        self.assertEqual(kwargs["json"], {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": "971501234567",
            "type": "text",
            "text": {"preview_url": False, "body": "Hello, your quote is 500 AED."},
        })

    async def test_missing_credentials(self):
        skill = WhatsAppReplySkill(access_token="", phone_number_id="")
        with patch.dict("os.environ", {"WHATSAPP_TOKEN": "", "WHATSAPP_PHONE_NUMBER_ID": ""}):
            skill = WhatsAppReplySkill()
        result = await skill.send_message(to_number="+971501234567", body="Test message")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "WHATSAPP_CREDENTIALS_MISSING")

    async def test_retries_rate_limit_then_succeeds(self):
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_post.side_effect = [
                _response(429, {"error": {"message": "rate limit"}}),
                _response(200, {"messages": [{"id": "wamid.after_retry"}]}),
            ]
            result = await self.skill.send_message(to_number="+971501234567", body="Hi")
        self.assertTrue(result["success"])
        self.assertEqual(result["message_sid"], "wamid.after_retry")
        self.assertEqual(mock_post.call_count, 2)

    async def test_fatal_error_is_not_retried(self):
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = _response(400, {"error": {"message": "Invalid parameter"}})
            result = await self.skill.send_message(to_number="+971501234567", body="Hi")
        self.assertFalse(result["success"])
        self.assertEqual(mock_post.call_count, 1)

    async def test_long_reply_is_split_at_meta_text_limit(self):
        long_text = "\n\n".join(f"Paragraph {i}: " + "x" * 1500 for i in range(6))
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_post.side_effect = [
                _response(200, {"messages": [{"id": f"wamid.part{i}"}]}) for i in range(10)
            ]
            result = await self.skill.send_message(to_number="+971501234567", body=long_text)

        bodies = [c.kwargs["json"]["text"]["body"] for c in mock_post.call_args_list]
        self.assertTrue(result["success"])
        self.assertGreater(len(bodies), 1)
        self.assertTrue(all(len(b) <= 4096 for b in bodies))
        self.assertEqual("".join(bodies).replace("\n", ""), long_text.replace("\n", ""))
        self.assertEqual(split_message_text("short"), ["short"])

    async def test_download_media_by_id(self):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = [
                _response(200, {"url": "https://lookaside.fbsbx.com/media/abc", "mime_type": "audio/ogg"}),
                _response(200, content=b"OGG-BYTES"),
            ]
            result = await self.skill.download_media("MEDIA_ID_1")

        self.assertEqual(result, {"success": True, "content": b"OGG-BYTES", "mime_type": "audio/ogg"})
        urls = [c.args[0] for c in mock_get.call_args_list]
        self.assertEqual(urls, ["https://graph.facebook.com/v23.0/MEDIA_ID_1", "https://lookaside.fbsbx.com/media/abc"])
        for call in mock_get.call_args_list:
            self.assertEqual(call.kwargs["headers"], {"Authorization": "Bearer TEST_TOKEN"})

    async def test_upload_and_send_document(self):
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = [
                _response(200, {"id": "UPLOADED_MEDIA_7"}),
                _response(200, {"messages": [{"id": "wamid.doc_1"}]}),
            ]
            upload = await self.skill.upload_media(b"%PDF-1.4", "application/pdf", "INV-1001.pdf")
            sent = await self.skill.send_document("+971501234567", upload["media_id"], "INV-1001.pdf", "Draft invoice")

        self.assertEqual(upload, {"success": True, "media_id": "UPLOADED_MEDIA_7"})
        self.assertEqual(sent, {"success": True, "message_sid": "wamid.doc_1"})
        upload_call, send_call = mock_post.call_args_list
        self.assertEqual(upload_call.args[0], "https://graph.facebook.com/v23.0/109876543210/media")
        self.assertEqual(upload_call.kwargs["files"]["file"], ("INV-1001.pdf", b"%PDF-1.4", "application/pdf"))
        self.assertEqual(send_call.kwargs["json"]["document"],
                         {"id": "UPLOADED_MEDIA_7", "filename": "INV-1001.pdf", "caption": "Draft invoice"})


if __name__ == "__main__":
    unittest.main()
