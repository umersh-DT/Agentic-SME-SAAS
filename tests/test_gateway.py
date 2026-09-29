import base64
import hashlib
import hmac
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
import yaml

from src.core.storage_models import TenantDatabaseManager
from src.gateway.dispatcher import TenantWorkerDispatcher
from src.gateway.main import app
from src.gateway.stripe_billing import provision_tenant_storage
from src.gateway.twilio_webhook import (
    StrictTenantDirectory,
    dispatcher as global_dispatcher,
    tenant_directory,
)
from src.skills.whatsapp_reply import WhatsAppReplySkill
from src.utils.security import TenantConfig, normalize_phone_number


class TestGatewayWebhook(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.auth_token = "test_auth_token_secret_123"
        self.stripe_secret = "whsec_mock_test_secret_9988"
        self.webhook_url = "http://testserver/webhook/whatsapp"

    def tearDown(self):
        tenant_directory.reload_tenants()

    def _generate_twilio_signature(self, url: str, params: dict, token: str) -> str:
        concatenated = url + "".join(f"{k}{v}" for k, v in sorted(params.items()))
        return base64.b64encode(
            hmac.new(token.encode("utf-8"), concatenated.encode("utf-8"), hashlib.sha1).digest()
        ).decode("utf-8")

    # =========================================================================
    # 1. CORE GATEWAY & DIRECTORY RESOLUTION TESTS
    # =========================================================================

    def test_health_check(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "healthy")
        self.assertEqual(data["service"], "agentic-gateway")

    def test_phone_normalization_and_e164_validation(self):
        """Verifies E.164 normalization logic and validation via TenantConfig."""
        self.assertEqual(normalize_phone_number("whatsapp:+971501234567"), "+971501234567")
        self.assertEqual(normalize_phone_number("+971 50 123 4567"), "+971501234567")
        self.assertEqual(normalize_phone_number(" +971-50-1234567 "), "+971501234567")

        with self.assertRaises(ValueError):
            TenantConfig(
                tenant_id="test_invalid",
                business_name="Test",
                plan_tier="starter",
                owner_phone="0501112233",
                whatsapp_number="+971501234567",
            )

    def test_strict_tenant_directory_live_resolution_and_collision(self):
        with tempfile.NamedTemporaryFile("w+", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(
                {
                    "tenants": [
                        {
                            "tenant_id": "tenant_alpha_01",
                            "business_name": "Alpha Co",
                            "plan_tier": "starter",
                            "owner_phone": "+971501111111",
                            "whatsapp_number": "+971502222222",
                            "staff_phones": ["+971502222222"],
                        },
                        {
                            "tenant_id": "tenant_beta_02",
                            "business_name": "Beta Co",
                            "plan_tier": "pro",
                            "owner_phone": "+971503333333",
                            "whatsapp_number": "+971502222222",
                            "staff_phones": ["+971502222222"],
                        },
                    ]
                },
                f,
            )
            temp_path = f.name

        try:
            directory = StrictTenantDirectory(config_path=temp_path)
            self.assertEqual(directory.resolve_sender("+971501111111"), "tenant_alpha_01")
            self.assertEqual(directory.resolve_sender("+971503333333"), "tenant_beta_02")
            self.assertIsNone(directory.resolve_sender("+971502222222"))
            self.assertIn("+971502222222", directory.collided_phones)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    # =========================================================================
    # 2. TWILIO INGRESS WEBHOOK SECURITY & ISOLATED CONTRACT TESTS
    # =========================================================================

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

    def test_twilio_webhook_deduplication(self):
        """Ensures that repeated messages with identical MessageSid are dropped."""
        payload = {
            "From": "+971501234567",
            "To": "+14155238886",
            "Body": "First attempt",
            "MessageSid": "SM_dedup_unique_sid_test",
        }
        sig = self._generate_twilio_signature(self.webhook_url, payload, self.auth_token)

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ", {"TWILIO_AUTH_TOKEN": self.auth_token, "TENANTS_DATA_DIR": temp_dir}
        ), patch("src.gateway.twilio_webhook.dispatcher.process_incoming_message", new_callable=AsyncMock):
            resp1 = self.client.post("/webhook/whatsapp", data=payload, headers={"X-Twilio-Signature": sig})
            self.assertEqual(resp1.status_code, 200)

            resp2 = self.client.post("/webhook/whatsapp", data=payload, headers={"X-Twilio-Signature": sig})
            self.assertEqual(resp2.status_code, 200)
            self.assertEqual(resp2.headers.get("X-Dedup-Dropped"), "true")

    def test_twilio_webhook_unregistered_sender_rejected(self):
        """Verifies unknown sender receives rejection TwiML without invoking agent loop."""
        payload = {
            "From": "+971509990000",
            "To": "+14155238886",
            "Body": "Hello unlisted",
            "MessageSid": "SM_unregistered_001",
        }
        sig = self._generate_twilio_signature(self.webhook_url, payload, self.auth_token)

        with patch.dict("os.environ", {"TWILIO_AUTH_TOKEN": self.auth_token}):
            response = self.client.post("/webhook/whatsapp", data=payload, headers={"X-Twilio-Signature": sig})
            self.assertEqual(response.status_code, 200)
            self.assertIn("<Message>This phone number is not registered", response.text)

    def test_twilio_signed_text_message_registered_sender(self):
        """Isolated test: verifies webhook validates signature, persists, and enqueues dispatcher."""
        payload = {
            "From": "+971501234567",
            "To": "+14155238886",
            "Body": "Hello assistant",
            "MessageSid": "SM_valid_registered_1",
        }
        sig = self._generate_twilio_signature(self.webhook_url, payload, self.auth_token)

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ", {"TWILIO_AUTH_TOKEN": self.auth_token, "TENANTS_DATA_DIR": temp_dir}
        ), patch(
            "src.gateway.twilio_webhook.dispatcher.process_incoming_message", new_callable=AsyncMock
        ) as mock_dispatch:
            response = self.client.post("/webhook/whatsapp", data=payload, headers={"X-Twilio-Signature": sig})
            self.assertEqual(response.status_code, 200)
            self.assertIn("<Response></Response>", response.text)
            mock_dispatch.assert_called_once()

    def test_twilio_signed_voice_note_empty_body(self):
        """Isolated test: non-text/media-only messages trigger advisory notice without calling agent."""
        payload = {
            "From": "+971501234567",
            "To": "+14155238886",
            "Body": "",
            "NumMedia": "1",
            "MessageSid": "SM_voice_note_empty_body_1",
        }
        sig = self._generate_twilio_signature(self.webhook_url, payload, self.auth_token)

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ", {"TWILIO_AUTH_TOKEN": self.auth_token, "TENANTS_DATA_DIR": temp_dir}
        ), patch(
            "src.gateway.twilio_webhook.reply_skill.send_reply", new_callable=AsyncMock
        ) as mock_reply, patch(
            "src.gateway.twilio_webhook.dispatcher.process_incoming_message", new_callable=AsyncMock
        ) as mock_dispatch:
            response = self.client.post("/webhook/whatsapp", data=payload, headers={"X-Twilio-Signature": sig})
            self.assertEqual(response.status_code, 200)
            mock_reply.assert_called_once()
            mock_dispatch.assert_not_called()

    # =========================================================================
    # 3. END-TO-END WEBHOOK INTEGRATION TEST
    # =========================================================================

    def test_e2e_webhook_background_task_to_llm_and_reply(self):
        """Webhook -> Background task -> Mocked LLM -> Outbound reply -> token_usage in SQLite."""
        payload = {
            "From": "+971501234567",
            "To": "+14155238886",
            "Body": "What is our company deposit policy?",
            "MessageSid": "SM_e2e_full_chain_test_01",
        }
        sig = self._generate_twilio_signature(self.webhook_url, payload, self.auth_token)

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.tool_calls = None
        mock_resp.choices[0].message.content = "Standard deposit is 50% upfront."
        mock_resp.choices[0].message.to_dict.return_value = {
            "role": "assistant",
            "content": "Standard deposit is 50% upfront.",
        }
        mock_resp.usage.prompt_tokens = 80
        mock_resp.usage.completion_tokens = 20

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_yaml = os.path.join(temp_dir, "tenants.yaml")
            shutil.copy("config/tenants.yaml", temp_yaml)

            with patch.dict(
                "os.environ",
                {
                    "TWILIO_AUTH_TOKEN": self.auth_token,
                    "TENANTS_CONFIG_PATH": temp_yaml,
                    "TENANTS_DATA_DIR": temp_dir,
                },
            ), patch.object(global_dispatcher, "data_root", temp_dir), patch.object(
                global_dispatcher.reply_skill, "send_reply", new_callable=AsyncMock
            ) as mock_send_reply, patch(
                "litellm.acompletion", return_value=mock_resp
            ):
                tenant_directory.reload_tenants()
                mock_send_reply.return_value = {"success": True, "message_sid": "SM_mock_e2e_reply"}

                response = self.client.post("/webhook/whatsapp", data=payload, headers={"X-Twilio-Signature": sig})
                self.assertEqual(response.status_code, 200)

                mock_send_reply.assert_called_once()
                self.assertEqual(mock_send_reply.call_args.kwargs.get("to_number"), "+971501234567")
                self.assertIn("Standard deposit is 50% upfront", mock_send_reply.call_args.kwargs.get("message"))

                db_mgr = TenantDatabaseManager(tenant_id="tenant_curtains_001", base_dir=temp_dir)
                with db_mgr._get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT status FROM inbound_messages WHERE message_sid = ?;",
                        (payload["MessageSid"],),
                    )
                    row = cursor.fetchone()
                    self.assertIsNotNone(row)
                    self.assertEqual(row["status"], "completed")

                    cursor.execute(
                        "SELECT total_tokens, cost_usd FROM token_usage WHERE message_sid = ?;",
                        (payload["MessageSid"],),
                    )
                    token_row = cursor.fetchone()
                    self.assertIsNotNone(token_row)
                    self.assertEqual(token_row["total_tokens"], 100)
                    self.assertGreater(token_row["cost_usd"], 0.0)

    # =========================================================================
    # 4. STRIPE BILLING WEBHOOK & PROVISIONING TESTS
    # =========================================================================

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
        payload = json.dumps(
            {
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "metadata": {
                            "tenant_id": "../../etc/malicious",
                            "plan_tier": "pro",
                            "owner_phone": "+971501112233",
                        }
                    }
                },
            }
        ).encode("utf-8")
        sig = hmac.new(
            self.stripe_secret.encode("utf-8"), f"{ts}.".encode("utf-8") + payload, hashlib.sha256
        ).hexdigest()
        with patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": self.stripe_secret}):
            response = self.client.post(
                "/webhook/stripe",
                content=payload,
                headers={"Stripe-Signature": f"t={ts},v1={sig}"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json().get("status"), "ignored_malformed_tenant_id")

    def test_stripe_internal_provisioning_failure_returns_500(self):
        ts = str(int(time.time()))
        payload = json.dumps(
            {
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "metadata": {
                            "tenant_id": "tenant_broken_001",
                            "plan_tier": "pro",
                            "owner_phone": "+971508889900",
                        }
                    }
                },
            }
        ).encode("utf-8")
        sig = hmac.new(
            self.stripe_secret.encode("utf-8"), f"{ts}.".encode("utf-8") + payload, hashlib.sha256
        ).hexdigest()
        with patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": self.stripe_secret}), patch(
            "src.gateway.stripe_billing.provision_tenant_storage", side_effect=IOError("Disk write failed")
        ):
            response = self.client.post(
                "/webhook/stripe",
                content=payload,
                headers={"Stripe-Signature": f"t={ts},v1={sig}"},
            )
            self.assertEqual(response.status_code, 500)

    def test_stripe_valid_checkout_provisions_and_registers_hermetically(self):
        ts = str(int(time.time()))
        tenant_id = "tenant_hermetic_99"
        phone = "+971509998877"
        payload = json.dumps(
            {
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
                        },
                    }
                },
            }
        ).encode("utf-8")
        sig = hmac.new(
            self.stripe_secret.encode("utf-8"), f"{ts}.".encode("utf-8") + payload, hashlib.sha256
        ).hexdigest()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_yaml = os.path.join(temp_dir, "tenants.yaml")
            with open(temp_yaml, "w", encoding="utf-8") as f:
                yaml.safe_dump({"tenants": []}, f)

            with patch.dict(
                "os.environ",
                {
                    "STRIPE_WEBHOOK_SECRET": self.stripe_secret,
                    "TENANTS_CONFIG_PATH": temp_yaml,
                    "TENANTS_DATA_DIR": temp_dir,
                },
            ):
                response = self.client.post(
                    "/webhook/stripe",
                    content=payload,
                    headers={"Stripe-Signature": f"t={ts},v1={sig}"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json().get("status"), "success")
                self.assertEqual(tenant_directory.resolve_sender(phone), tenant_id)

    def test_stripe_checkout_with_existing_phone_rejected_without_disrupting_existing_tenant(self):
        ts = str(int(time.time()))
        existing_phone = "+971501234567"
        payload = json.dumps(
            {
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "metadata": {
                            "tenant_id": "tenant_hijacker_99",
                            "business_name": "Hijack Attempt Ltd",
                            "plan_tier": "pro",
                            "owner_phone": existing_phone,
                        }
                    }
                },
            }
        ).encode("utf-8")
        sig = hmac.new(
            self.stripe_secret.encode("utf-8"), f"{ts}.".encode("utf-8") + payload, hashlib.sha256
        ).hexdigest()

        with patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": self.stripe_secret}):
            response = self.client.post(
                "/webhook/stripe",
                content=payload,
                headers={"Stripe-Signature": f"t={ts},v1={sig}"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json().get("status"), "ignored_phone_already_registered")
            self.assertEqual(tenant_directory.resolve_sender(existing_phone), "tenant_curtains_001")

    def test_stripe_checkout_malformed_phone_returns_200_and_does_not_corrupt_yaml(self):
        ts = str(int(time.time()))
        payload = json.dumps(
            {
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "metadata": {
                            "tenant_id": "tenant_bad_phone_01",
                            "business_name": "Bad Phone Business",
                            "plan_tier": "starter",
                            "owner_phone": "0501112233",
                        }
                    }
                },
            }
        ).encode("utf-8")
        sig = hmac.new(
            self.stripe_secret.encode("utf-8"), f"{ts}.".encode("utf-8") + payload, hashlib.sha256
        ).hexdigest()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_yaml = os.path.join(temp_dir, "tenants.yaml")
            with open(temp_yaml, "w", encoding="utf-8") as f:
                yaml.safe_dump({"tenants": []}, f)

            with patch.dict(
                "os.environ",
                {
                    "STRIPE_WEBHOOK_SECRET": self.stripe_secret,
                    "TENANTS_CONFIG_PATH": temp_yaml,
                    "TENANTS_DATA_DIR": temp_dir,
                },
            ):
                response = self.client.post(
                    "/webhook/stripe",
                    content=payload,
                    headers={"Stripe-Signature": f"t={ts},v1={sig}"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json().get("status"), "ignored_invalid_customer_data")

                with open(temp_yaml, "r", encoding="utf-8") as f:
                    content = yaml.safe_load(f)
                    self.assertEqual(len(content.get("tenants", [])), 0)

    def test_stripe_checkout_existing_tenant_updates_only_billing_and_preserves_attributes(self):
        """Verifies existing tenant checkout updates billing fields in isolated config."""
        ts = str(int(time.time()))
        tenant_id = "tenant_curtains_001"
        verified_phone = "+971501234567"

        payload = json.dumps(
            {
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "customer": "cus_upgrade_999",
                        "subscription": "sub_upgrade_888",
                        "metadata": {
                            "tenant_id": tenant_id,
                            "business_name": "Wiped Name Ltd",
                            "plan_tier": "enterprise",
                            "owner_phone": verified_phone,
                        },
                    }
                },
            }
        ).encode("utf-8")
        sig = hmac.new(
            self.stripe_secret.encode("utf-8"), f"{ts}.".encode("utf-8") + payload, hashlib.sha256
        ).hexdigest()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_yaml = os.path.join(temp_dir, "tenants.yaml")
            shutil.copy("config/tenants.yaml", temp_yaml)

            with patch.dict(
                "os.environ",
                {
                    "STRIPE_WEBHOOK_SECRET": self.stripe_secret,
                    "TENANTS_CONFIG_PATH": temp_yaml,
                    "TENANTS_DATA_DIR": temp_dir,
                },
            ):
                tenant_directory.reload_tenants()
                try:
                    response = self.client.post(
                        "/webhook/stripe",
                        content=payload,
                        headers={"Stripe-Signature": f"t={ts},v1={sig}"},
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json().get("status"), "success")

                    t_obj = tenant_directory.tenants[tenant_id]
                    self.assertEqual(t_obj.plan_tier, "enterprise")
                    self.assertEqual(t_obj.stripe_customer_id, "cus_upgrade_999")
                    self.assertEqual(t_obj.business_name, "Luxe Curtain Interiors")
                    self.assertIn("seo_manager", t_obj.enabled_skills)
                finally:
                    tenant_directory.reload_tenants()

    def test_stripe_checkout_existing_tenant_with_different_phone_rejected(self):
        ts = str(int(time.time()))
        tenant_id = "tenant_curtains_001"
        hijack_phone = "+971509990000"

        payload = json.dumps(
            {
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "metadata": {
                            "tenant_id": tenant_id,
                            "business_name": "Luxe Curtain Interiors",
                            "plan_tier": "enterprise",
                            "owner_phone": hijack_phone,
                        }
                    }
                },
            }
        ).encode("utf-8")
        sig = hmac.new(
            self.stripe_secret.encode("utf-8"), f"{ts}.".encode("utf-8") + payload, hashlib.sha256
        ).hexdigest()

        with patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": self.stripe_secret}):
            response = self.client.post(
                "/webhook/stripe",
                content=payload,
                headers={"Stripe-Signature": f"t={ts},v1={sig}"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json().get("status"), "ignored_phone_mismatch_for_existing_tenant")
            self.assertEqual(tenant_directory.resolve_sender("+971501234567"), tenant_id)


if __name__ == "__main__":
    unittest.main()