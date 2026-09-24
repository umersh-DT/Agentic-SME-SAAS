import base64
import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl

from fastapi.testclient import TestClient

from src.gateway.main import app


class TestGatewayWebhook(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.auth_token = "test_auth_token_secret_123"
        self.stripe_secret = "whsec_mock_test_secret_9988"

    def test_health_check(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "healthy")
        self.assertEqual(data["service"], "agentic-gateway")

    def test_twilio_fail_closed_without_token(self):
        """Verifies that webhook rejects requests with 403 if TWILIO_AUTH_TOKEN is unset."""
        with patch.dict("os.environ", {"TWILIO_AUTH_TOKEN": ""}):
            response = self.client.post(
                "/webhook/whatsapp",
                data={
                    "From": "whatsapp:+971501112233",
                    "To": "whatsapp:+14155238886",
                    "Body": "Hello",
                    "MessageSid": "SM_unauthed_test",
                },
            )
            self.assertEqual(response.status_code, 403)

    def test_stripe_fail_closed_without_secret(self):
        """Verifies that webhook rejects requests with 400 if STRIPE_WEBHOOK_SECRET is unset."""
        with patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": ""}):
            response = self.client.post(
                "/webhook/stripe",
                content=b'{"type":"checkout.session.completed"}',
                headers={"Stripe-Signature": "t=123,v1=fake"},
            )
            self.assertEqual(response.status_code, 400)

    def test_stripe_path_traversal_rejected(self):
        """Verifies that an invalid tenant_id (e.g. traversal attempt) is rejected with 400."""
        ts = str(int(time.time()))
        payload = json.dumps({
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "metadata": {
                        "tenant_id": "../../etc/malicious",
                        "plan_tier": "pro",
                    }
                }
            }
        }).encode("utf-8")

        signed_payload = f"{ts}.".encode("utf-8") + payload
        sig = hmac.new(self.stripe_secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
        sig_header = f"t={ts},v1={sig}"

        with patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": self.stripe_secret}):
            response = self.client.post(
                "/webhook/stripe",
                content=payload,
                headers={"Stripe-Signature": sig_header},
            )
            self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()