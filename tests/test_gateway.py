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
import yaml

from src.gateway.main import app
from src.gateway.twilio_webhook import StrictTenantDirectory, tenant_directory


class TestGatewayWebhook(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.auth_token = "test_auth_token_secret_123"
        self.stripe_secret = "whsec_mock_test_secret_9988"
        self.webhook_url = "http://testserver/webhook/whatsapp"

    def _generate_twilio_signature(self, url: str, params: dict, token: str) -> str:
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

    def test_strict_tenant_directory_live_resolution_and_collision(self):
        """Hermetically tests real StrictTenantDirectory loading, matching, and collision drops without mocks."""
        with tempfile.NamedTemporaryFile("w+", suffix=".yaml", delete=False) as f:
            yaml.safe_dump({
                "tenants": [
                    {
                        "tenant_id": "tenant_alpha_01",
                        "business_name": "Alpha Co",
                        "owner_phone": "+971501111111",
                        "staff_phones": ["+971502222222"],
                    },
                    {
                        "tenant_id": "tenant_beta_02",
                        "business_name": "Beta Co",
                        "owner_phone": "+971503333333",
                        "staff_phones": ["+971502222222"],  # Collision on +971502222222!
                    }
                ]
            }, f)
            temp_path = f.name

        try:
            directory = StrictTenantDirectory(config_path=temp_path)
            # Distinct numbers resolve
            self.assertEqual(directory.resolve_sender("+971501111111"), "tenant_alpha_01")
            self.assertEqual(directory.resolve_sender("+971503333333"), "tenant_beta_02")
            # Collided phone must be dropped from BOTH tenants
            self.assertIsNone(directory.resolve_sender("+971502222222"))
            self.assertIn("+971502222222", directory.collided_phones)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_twilio_signed_text_message_registered_sender(self):
        """Verifies that an unmocked registered sender in tenants.yaml is routed with 200."""
        payload = {
            "From": "+971501234567",
            "To": "+17372508034",
            "Body": "Hello assistant",
            "MessageSid": "SM_valid_registered_1",
        }
        sig = self._generate_twilio_signature(self.webhook_url, payload, self.auth_token)
        with patch.dict("os.environ", {"TWILIO_AUTH_TOKEN": self.auth_token}):
            response = self.client.post("/webhook/whatsapp", data=payload, headers={"X-Twilio-Signature": sig})
            self.assertEqual(response.status_code, 200)
            self.assertIn("<Response></Response>", response.text)

    def test_twilio_signed_voice_note_empty_body(self):
        payload = {
            "From": "+971501234567",
            "To": "+17372508034",
            "Body": "",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/mock/audio.ogg",
            "MessageSid": "SM_voice_note_1",
        }
        sig = self._generate_twilio_signature(self.webhook_url, payload, self.auth_token)
        with patch.dict("os.environ", {"TWILIO_AUTH_TOKEN": self.auth_token}):
            response = self.client.post("/webhook/whatsapp", data=payload, headers={"X-Twilio-Signature": sig})
            self.assertEqual(response.status_code, 200)
            self.assertIn("<Response></Response>", response.text)

    def test_twilio_duplicate_message_dropped(self):
        """Verifies duplicate MessageSid is recognized and dropped via X-Dedup-Dropped header."""
        payload = {
            "From": "+971501234567",
            "To": "+17372508034",
            "Body": "Test Dedup",
            "MessageSid": "SM_dedup_unique_sid_test",
        }
        sig = self._generate_twilio_signature(self.webhook_url, payload, self.auth_token)
        with patch.dict("os.environ", {"TWILIO_AUTH_TOKEN": self.auth_token}):
            r1 = self.client.post("/webhook/whatsapp", data=payload, headers={"X-Twilio-Signature": sig})
            self.assertEqual(r1.status_code, 200)
            self.assertNotIn("X-Dedup-Dropped", r1.headers)

            r2 = self.client.post("/webhook/whatsapp", data=payload, headers={"X-Twilio-Signature": sig})
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(r2.headers.get("X-Dedup-Dropped"), "true")

    def test_twilio_unregistered_sender_discarded_gracefully(self):
        payload = {
            "From": "+99900000000",
            "To": "+17372508034",
            "Body": "Unknown ping",
            "MessageSid": "SM_unregistered_1",
        }
        sig = self._generate_twilio_signature(self.webhook_url, payload, self.auth_token)
        with patch.dict("os.environ", {"TWILIO_AUTH_TOKEN": self.auth_token}):
            response = self.client.post("/webhook/whatsapp", data=payload, headers={"X-Twilio-Signature": sig})
            self.assertEqual(response.status_code, 200)
            self.assertIn("<Message>This phone number is not registered", response.text)

    def test_stripe_fail_closed_without_secret(self):
        with patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": ""}):
            response = self.client.post(
                "/webhook/stripe",
                content=b'{"type":"checkout.session.completed"}',
                headers={"Stripe-Signature": "t=123,v1=fake"},
            )
            self.assertEqual(response.status_code, 400)

    def test_stripe_path_traversal_rejected(self):
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
        sig = hmac.new(self.stripe_secret.encode("utf-8"), f"{ts}.".encode("utf-8") + payload, hashlib.sha256).hexdigest()
        with patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": self.stripe_secret}):
            response = self.client.post(
                "/webhook/stripe",
                content=payload,
                headers={"Stripe-Signature": f"t={ts},v1={sig}"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json().get("status"), "ignored_malformed_tenant_id")

    def test_stripe_internal_provisioning_failure_returns_500(self):
        """Verifies that an internal provisioning error returns HTTP 500 so Stripe retries."""
        ts = str(int(time.time()))
        payload = json.dumps({
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "metadata": {
                        "tenant_id": "tenant_broken_001",
                        "plan_tier": "pro",
                    }
                }
            }
        }).encode("utf-8")
        sig = hmac.new(self.stripe_secret.encode("utf-8"), f"{ts}.".encode("utf-8") + payload, hashlib.sha256).hexdigest()
        with patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": self.stripe_secret}), \
             patch("src.gateway.stripe_billing.provision_tenant_storage", side_effect=IOError("Disk write failed")):
            response = self.client.post(
                "/webhook/stripe",
                content=payload,
                headers={"Stripe-Signature": f"t={ts},v1={sig}"},
            )
            self.assertEqual(response.status_code, 500)

    def test_stripe_valid_checkout_provisions_and_registers_hermetically(self):
        """Verifies that a valid checkout creates storage and updates tenants.yaml so tenant is routable."""
        ts = str(int(time.time()))
        tenant_id = "tenant_hermetic_99"
        phone = "+971509998877"
        payload = json.dumps({
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "customer": "cus_live_mock_1122",
                    "subscription": "sub_live_mock_3344",
                    "metadata": {
                        "tenant_id": tenant_id,
                        "business_name": "Hermetic Enterprise",
                        "plan_tier": "enterprise",
                        "owner_phone": phone,
                    }
                }
            }
        }).encode("utf-8")
        sig = hmac.new(self.stripe_secret.encode("utf-8"), f"{ts}.".encode("utf-8") + payload, hashlib.sha256).hexdigest()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_yaml = os.path.join(temp_dir, "tenants.yaml")
            with open(temp_yaml, "w", encoding="utf-8") as f:
                yaml.safe_dump({"tenants": []}, f)

            with patch.dict("os.environ", {
                "STRIPE_WEBHOOK_SECRET": self.stripe_secret,
                "TENANTS_CONFIG_PATH": temp_yaml,
                "TENANTS_DATA_DIR": temp_dir,
            }):
                response = self.client.post(
                    "/webhook/stripe",
                    content=payload,
                    headers={"Stripe-Signature": f"t={ts},v1={sig}"},
                )
                self.assertEqual(response.status_code, 200)

                # Verify SQLite database was provisioned in temp_dir
                expected_db = os.path.join(temp_dir, f"{tenant_id}.sqlite")
                self.assertTrue(os.path.exists(expected_db))

                # Verify tenant was added to tenants.yaml and in-memory directory
                self.assertEqual(tenant_directory.resolve_sender(phone), tenant_id)


if __name__ == "__main__":
    unittest.main()