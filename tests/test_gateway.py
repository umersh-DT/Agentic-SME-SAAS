import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.gateway.main import app


class TestGatewayWebhook(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.auth_token = "test_auth_token_secret_123"
        self.stripe_secret = "whsec_mock_test_secret_9988"
        self.webhook_url = "http://testserver/webhook/whatsapp"

    def _generate_twilio_signature(self, url: str, params: dict, token: str) -> str:
        """Helper to compute valid Twilio HMAC-SHA1 signature matching keep_blank_values=True."""
        concatenated = url + "".join(f"{k}{v}" for k, v in sorted(params.items()))
        return base64.b64encode(
            hmac.new(token.encode("utf-8"), concatenated.encode("utf-8"), hashlib.sha1).digest()
        ).decode("utf-8")

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

    def test_twilio_signed_text_message_registered_sender(self):
        """Verifies that a validly signed message from a registered sender returns 200."""
        payload = {
            "From": "+971501234567",
            "To": "+17372508034",
            "Body": "Hello assistant",
            "MessageSid": "SM_valid_registered_1",
        }
        sig = self._generate_twilio_signature(self.webhook_url, payload, self.auth_token)
        with patch.dict("os.environ", {"TWILIO_AUTH_TOKEN": self.auth_token}), \
             patch("src.gateway.twilio_webhook.tenant_directory.resolve_sender", return_value="tenant_curtains_001"):
            response = self.client.post(
                "/webhook/whatsapp",
                data=payload,
                headers={"X-Twilio-Signature": sig},
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("<Response></Response>", response.text)

    def test_twilio_signed_voice_note_empty_body(self):
        """Verifies that a signed voice note with Body='' is accepted (not 403) via keep_blank_values=True."""
        payload = {
            "From": "+971501234567",
            "To": "+17372508034",
            "Body": "",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/mock/audio.ogg",
            "MessageSid": "SM_voice_note_1",
        }
        sig = self._generate_twilio_signature(self.webhook_url, payload, self.auth_token)
        with patch.dict("os.environ", {"TWILIO_AUTH_TOKEN": self.auth_token}), \
             patch("src.gateway.twilio_webhook.tenant_directory.resolve_sender", return_value="tenant_curtains_001"):
            response = self.client.post(
                "/webhook/whatsapp",
                data=payload,
                headers={"X-Twilio-Signature": sig},
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("<Response></Response>", response.text)

    def test_twilio_duplicate_message_dropped(self):
        """Verifies that duplicate MessageSid entries are dropped by deduplication cache."""
        payload = {
            "From": "+971501234567",
            "To": "+17372508034",
            "Body": "Test Dedup",
            "MessageSid": "SM_dedup_unique_sid",
        }
        sig = self._generate_twilio_signature(self.webhook_url, payload, self.auth_token)
        with patch.dict("os.environ", {"TWILIO_AUTH_TOKEN": self.auth_token}), \
             patch("src.gateway.twilio_webhook.tenant_directory.resolve_sender", return_value="tenant_curtains_001"):
            r1 = self.client.post("/webhook/whatsapp", data=payload, headers={"X-Twilio-Signature": sig})
            self.assertEqual(r1.status_code, 200)

            r2 = self.client.post("/webhook/whatsapp", data=payload, headers={"X-Twilio-Signature": sig})
            self.assertEqual(r2.status_code, 200)

    def test_twilio_unregistered_sender_discarded_gracefully(self):
        """Verifies that an unregistered sender gets polite rejection TwiML without touching storage."""
        payload = {
            "From": "+99900000000",
            "To": "+17372508034",
            "Body": "Unknown ping",
            "MessageSid": "SM_unregistered_1",
        }
        sig = self._generate_twilio_signature(self.webhook_url, payload, self.auth_token)
        with patch.dict("os.environ", {"TWILIO_AUTH_TOKEN": self.auth_token}), \
             patch("src.gateway.twilio_webhook.tenant_directory.resolve_sender", return_value=None):
            response = self.client.post("/webhook/whatsapp", data=payload, headers={"X-Twilio-Signature": sig})
            self.assertEqual(response.status_code, 200)
            self.assertIn("<Message>This phone number is not registered", response.text)

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
        """Verifies that malformed tenant_id logs security reject and returns 200 to halt Stripe retries."""
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
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data.get("status"), "ignored_malformed_tenant_id")

    def test_stripe_valid_checkout_provisioning_hermetic(self):
        """Verifies that a valid checkout session provisions tenant storage in a temporary directory."""
        ts = str(int(time.time()))
        payload = json.dumps({
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "metadata": {
                        "tenant_id": "tenant_test_valid_88",
                        "plan_tier": "starter",
                    }
                }
            }
        }).encode("utf-8")
        sig = hmac.new(self.stripe_secret.encode("utf-8"), f"{ts}.".encode("utf-8") + payload, hashlib.sha256).hexdigest()
        with patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": self.stripe_secret}), \
             patch("src.gateway.stripe_billing.provision_tenant_storage") as mock_prov:
            response = self.client.post(
                "/webhook/stripe",
                content=payload,
                headers={"Stripe-Signature": f"t={ts},v1={sig}"},
            )
            self.assertEqual(response.status_code, 200)
            mock_prov.assert_called_once_with(tenant_id="tenant_test_valid_88", plan_tier="starter")


if __name__ == "__main__":
    unittest.main()