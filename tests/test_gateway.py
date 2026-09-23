import hashlib
import hmac
import json
import time
import unittest
from fastapi.testclient import TestClient

from src.gateway.main import app
from src.gateway.twilio_webhook import dedup_cache, verify_twilio_signature
from src.gateway.stripe_billing import verify_stripe_signature


class TestGatewayWebhook(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_health_check(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "healthy")
        self.assertEqual(data["service"], "agentic-gateway")
        self.assertEqual(data["phase"], 3)

    def test_twilio_signature_verification_logic(self):
        token = "test_auth_token_secret_12345"
        url = "https://staging.domain.com/webhook/whatsapp"
        params = {
            "MessageSid": "SM1234567890abcdef",
            "From": "whatsapp:+971501234567",
            "To": "whatsapp:+14155238886",
            "Body": "What is the price for living room curtains?",
        }

        # Generate valid HMAC-SHA1 signature
        sorted_keys = sorted(params.keys())
        sig_payload = url + "".join(f"{k}{params[k]}" for k in sorted_keys)
        import base64
        valid_sig = base64.b64encode(
            hmac.new(token.encode("utf-8"), sig_payload.encode("utf-8"), hashlib.sha1).digest()
        ).decode("utf-8")

        self.assertTrue(verify_twilio_signature(url, params, valid_sig, token))
        self.assertFalse(verify_twilio_signature(url, params, "invalid_sig", token))
        self.assertFalse(verify_twilio_signature(url, params, valid_sig, ""))

    def test_whatsapp_webhook_successful_dispatch(self):
        payload = {
            "MessageSid": "SM_test_unique_998811",
            "From": "whatsapp:+971501234567",
            "To": "whatsapp:+14155238886",
            "Body": "Can I get a quote for motorized blinds?",
        }
        response = self.client.post("/webhook/whatsapp", data=payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/xml", response.headers["content-type"])
        self.assertIn("<Response></Response>", response.text)

    def test_whatsapp_webhook_deduplication(self):
        sid = "SM_duplicate_check_774411"
        payload = {
            "MessageSid": sid,
            "From": "whatsapp:+971501234567",
            "To": "whatsapp:+14155238886",
            "Body": "Checking idempotency",
        }

        # First dispatch
        res1 = self.client.post("/webhook/whatsapp", data=payload)
        self.assertEqual(res1.status_code, 200)

        # Duplicate retry with identical MessageSid
        res2 = self.client.post("/webhook/whatsapp", data=payload)
        self.assertEqual(res2.status_code, 200)
        self.assertTrue(dedup_cache.is_duplicate(sid))

    def test_stripe_signature_verification_logic(self):
        secret = "whsec_test_secret_abc123"
        t = int(time.time())
        raw_body = json.dumps({"type": "checkout.session.completed"}).encode("utf-8")

        signed_content = f"{t}.".encode("utf-8") + raw_body
        v1_sig = hmac.new(secret.encode("utf-8"), signed_content, hashlib.sha256).hexdigest()
        valid_header = f"t={t},v1={v1_sig}"

        self.assertTrue(verify_stripe_signature(raw_body, valid_header, secret))
        self.assertFalse(verify_stripe_signature(raw_body, f"t={t},v1=bad_sig", secret))
        # Verify timestamp drift outside 300s window is rejected
        self.assertFalse(verify_stripe_signature(raw_body, f"t={t - 400},v1={v1_sig}", secret))

    def test_stripe_webhook_checkout_completed(self):
        payload = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "customer": "cus_test_998811",
                    "subscription": "sub_test_443322",
                    "metadata": {
                        "tenant_id": "tenant_photographer_001",
                        "plan_tier": "pro",
                    },
                }
            },
        }
        response = self.client.post("/webhook/stripe", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["event_type"], "checkout.session.completed")


if __name__ == "__main__":
    unittest.main()