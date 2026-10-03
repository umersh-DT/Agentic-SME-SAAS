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
from src.gateway.whatsapp_webhook import (
    StrictTenantDirectory,
    dispatcher as global_dispatcher,
    tenant_directory,
    to_e164,
)
from src.skills.whatsapp_reply import WhatsAppReplySkill
from src.utils.security import TenantConfig, normalize_phone_number


class TestGatewayWebhook(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.app_secret = "test_meta_app_secret_123"
        self.verify_token = "test_verify_token_456"
        self.phone_number_id = "109876543210"
        self.stripe_secret = "whsec_mock_test_secret_9988"
        self.meta_env = {
            "WHATSAPP_APP_SECRET": self.app_secret,
            "WHATSAPP_VERIFY_TOKEN": self.verify_token,
            "WHATSAPP_PHONE_NUMBER_ID": self.phone_number_id,
        }

    def tearDown(self):
        tenant_directory.reload_tenants()

    def _meta_payload(self, wa_from: str, message: dict, phone_number_id: str = None) -> bytes:
        """Builds a WhatsApp Cloud API webhook body in Meta's documented shape."""
        return json.dumps({
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "WABA_ID",
                "changes": [{
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "15550001111",
                            "phone_number_id": phone_number_id or self.phone_number_id,
                        },
                        "contacts": [{"profile": {"name": "Tester"}, "wa_id": wa_from}],
                        "messages": [dict({"from": wa_from, "timestamp": "1730000000"}, **message)],
                    },
                }],
            }],
        }).encode("utf-8")

    def _text(self, wa_from: str, body: str, msg_id: str, **kw) -> bytes:
        return self._meta_payload(wa_from, {"id": msg_id, "type": "text", "text": {"body": body}}, **kw)

    def _sign(self, body: bytes, secret: str = None) -> dict:
        digest = hmac.new((secret or self.app_secret).encode("utf-8"), body, hashlib.sha256).hexdigest()
        return {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}

    def _post(self, body: bytes, headers: dict):
        return self.client.post("/webhook/whatsapp", content=body, headers=headers)

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
    # 2. META WHATSAPP INGRESS WEBHOOK SECURITY & CONTRACT TESTS
    # =========================================================================

    def test_meta_verification_echoes_raw_challenge(self):
        with patch.dict("os.environ", self.meta_env):
            ok = self.client.get("/webhook/whatsapp", params={
                "hub.mode": "subscribe", "hub.verify_token": self.verify_token, "hub.challenge": "1158201444",
            })
            bad = self.client.get("/webhook/whatsapp", params={
                "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1158201444",
            })
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.text, "1158201444")
        self.assertEqual(bad.status_code, 403)

    def test_meta_verification_fails_closed_without_verify_token(self):
        with patch.dict("os.environ", {"WHATSAPP_VERIFY_TOKEN": ""}):
            resp = self.client.get("/webhook/whatsapp", params={
                "hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "1",
            })
        self.assertEqual(resp.status_code, 403)

    def test_meta_post_fails_closed_without_app_secret(self):
        body = self._text("971501234567", "Hello", "wamid.unauthed")
        with patch.dict("os.environ", {"WHATSAPP_APP_SECRET": ""}):
            resp = self._post(body, self._sign(body))
        self.assertEqual(resp.status_code, 403)

    def test_meta_post_rejects_bad_or_missing_signature(self):
        body = self._text("971501234567", "Hello", "wamid.badsig")
        with patch.dict("os.environ", self.meta_env), patch(
            "src.gateway.whatsapp_webhook.dispatcher.process_incoming_message", new_callable=AsyncMock
        ) as mock_dispatch:
            wrong_secret = self._post(body, self._sign(body, secret="not_the_secret"))
            missing = self._post(body, {"Content-Type": "application/json"})
            tampered = self._post(body.replace(b"Hello", b"Hacked"), self._sign(body))
        self.assertEqual([wrong_secret.status_code, missing.status_code, tampered.status_code], [403, 403, 403])
        mock_dispatch.assert_not_called()

    def test_sender_number_is_normalized_to_e164(self):
        self.assertEqual(to_e164("971501234567"), "+971501234567")
        self.assertEqual(to_e164("+971501234567"), "+971501234567")

    def test_meta_status_updates_are_ignored(self):
        body = json.dumps({
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": self.phone_number_id},
                "statuses": [{"id": "wamid.out1", "status": "delivered", "recipient_id": "971501234567"}],
            }}]}],
        }).encode()
        with patch.dict("os.environ", self.meta_env), patch(
            "src.gateway.whatsapp_webhook.dispatcher.process_incoming_message", new_callable=AsyncMock
        ) as mock_dispatch, patch(
            "src.gateway.whatsapp_webhook.reply_skill.send_reply", new_callable=AsyncMock
        ) as mock_reply:
            resp = self._post(body, self._sign(body))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["messages"], [])
        mock_dispatch.assert_not_called()
        mock_reply.assert_not_called()

    def test_meta_payload_for_other_phone_number_id_is_ignored(self):
        body = self._text("971501234567", "Hello", "wamid.other_number", phone_number_id="999")
        with patch.dict("os.environ", self.meta_env), patch(
            "src.gateway.whatsapp_webhook.dispatcher.process_incoming_message", new_callable=AsyncMock
        ) as mock_dispatch:
            resp = self._post(body, self._sign(body))
        self.assertEqual(resp.json()["messages"], [])
        mock_dispatch.assert_not_called()

    def test_meta_webhook_deduplication(self):
        """A message id delivered twice is processed once."""
        body = self._text("971501234567", "First attempt", "wamid.dedup_unique")
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ", dict(self.meta_env, TENANTS_DATA_DIR=temp_dir)
        ), patch(
            "src.gateway.whatsapp_webhook.dispatcher.process_incoming_message", new_callable=AsyncMock
        ) as mock_dispatch:
            resp1 = self._post(body, self._sign(body))
            resp2 = self._post(body, self._sign(body))
        self.assertEqual(resp1.json()["messages"], ["dispatched"])
        self.assertEqual(resp2.json()["messages"], ["duplicate"])
        mock_dispatch.assert_called_once()

    def test_meta_unregistered_sender_gets_one_notice_per_day(self):
        """Unknown sender gets the 'not registered' notice as a normal outbound message, at most once per 24h."""
        with patch.dict("os.environ", self.meta_env), patch(
            "src.gateway.whatsapp_webhook.reply_skill.send_reply", new_callable=AsyncMock
        ) as mock_reply, patch(
            "src.gateway.whatsapp_webhook.dispatcher.process_incoming_message", new_callable=AsyncMock
        ) as mock_dispatch, patch(
            "src.gateway.whatsapp_webhook.rejection_limiter._last_reply", {}
        ):
            first = self._text("971509990000", "Hello unlisted", "wamid.unreg_1")
            second = self._text("971509990000", "Hello again", "wamid.unreg_2")
            r1 = self._post(first, self._sign(first))
            r2 = self._post(second, self._sign(second))
        self.assertEqual(r1.json()["messages"], ["unregistered_notified"])
        self.assertEqual(r2.json()["messages"], ["unregistered_silent"])
        mock_reply.assert_called_once()
        self.assertEqual(mock_reply.call_args.kwargs["to_number"], "+971509990000")
        self.assertIn("not registered", mock_reply.call_args.kwargs["message"])
        mock_dispatch.assert_not_called()

    def test_meta_signed_text_message_registered_sender(self):
        """Valid signed text from a registered sender is persisted and dispatched with the E.164 number."""
        body = self._text("971501234567", "Hello assistant", "wamid.valid_registered_1")
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ", dict(self.meta_env, TENANTS_DATA_DIR=temp_dir)
        ), patch(
            "src.gateway.whatsapp_webhook.dispatcher.process_incoming_message", new_callable=AsyncMock
        ) as mock_dispatch:
            response = self._post(body, self._sign(body))
            self.assertEqual(response.status_code, 200)
            mock_dispatch.assert_called_once()
            kwargs = mock_dispatch.call_args.kwargs
            self.assertEqual(kwargs["from_number"], "+971501234567")
            self.assertEqual(kwargs["message_sid"], "wamid.valid_registered_1")
            self.assertEqual(kwargs["body"], "Hello assistant")

            db_mgr = TenantDatabaseManager(tenant_id="tenant_curtains_001", base_dir=temp_dir)
            with db_mgr._get_connection() as conn:
                row = conn.execute(
                    "SELECT from_number, body, status FROM inbound_messages WHERE message_sid = ?;",
                    ("wamid.valid_registered_1",),
                ).fetchone()
            self.assertEqual((row["from_number"], row["body"], row["status"]), ("+971501234567", "Hello assistant", "pending"))

    def test_meta_voice_note_is_sent_for_transcription(self):
        """Voice notes are persisted with their media id and handed to the voice handler, not the text agent."""
        body = self._meta_payload("971501234567", {
            "id": "wamid.voice_1", "type": "audio",
            "audio": {"id": "MEDIA_ID_1", "mime_type": "audio/ogg; codecs=opus", "voice": True},
        })
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ", dict(self.meta_env, TENANTS_DATA_DIR=temp_dir)
        ), patch(
            "src.gateway.whatsapp_webhook.dispatcher.process_voice_message", new_callable=AsyncMock
        ) as mock_voice, patch(
            "src.gateway.whatsapp_webhook.dispatcher.process_incoming_message", new_callable=AsyncMock
        ) as mock_dispatch:
            response = self._post(body, self._sign(body))
            db_mgr = TenantDatabaseManager(tenant_id="tenant_curtains_001", base_dir=temp_dir)
            with db_mgr._get_connection() as conn:
                row = conn.execute(
                    "SELECT media_id, num_media FROM inbound_messages WHERE message_sid = 'wamid.voice_1';"
                ).fetchone()
        self.assertEqual(response.json()["messages"], ["voice_dispatched"])
        mock_dispatch.assert_not_called()
        mock_voice.assert_called_once()
        self.assertEqual(mock_voice.call_args.kwargs["media_id"], "MEDIA_ID_1")
        self.assertEqual(mock_voice.call_args.kwargs["from_number"], "+971501234567")
        self.assertEqual((row["media_id"], row["num_media"]), ("MEDIA_ID_1", 1))

    def test_meta_image_gets_not_supported_reply(self):
        body = self._meta_payload("971501234567", {"id": "wamid.img_1", "type": "image", "image": {"id": "IMG_1"}})
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ", dict(self.meta_env, TENANTS_DATA_DIR=temp_dir)
        ), patch(
            "src.gateway.whatsapp_webhook.reply_skill.send_reply", new_callable=AsyncMock
        ) as mock_reply, patch(
            "src.gateway.whatsapp_webhook.dispatcher.process_incoming_message", new_callable=AsyncMock
        ) as mock_dispatch:
            response = self._post(body, self._sign(body))
        self.assertEqual(response.json()["messages"], ["media_advisory"])
        self.assertIn("aren't supported yet", mock_reply.call_args.kwargs["message"])
        mock_dispatch.assert_not_called()

    # =========================================================================
    # 3. END-TO-END WEBHOOK INTEGRATION TEST
    # =========================================================================

    def test_e2e_webhook_background_task_to_llm_and_reply(self):
        """Signed Meta webhook -> Background task -> Mocked LLM -> Outbound reply -> token_usage in SQLite."""
        message_id = "wamid.e2e_full_chain_test_01"
        body = self._text("971501234567", "What is our company deposit policy?", message_id)

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
                dict(self.meta_env, TENANTS_CONFIG_PATH=temp_yaml, TENANTS_DATA_DIR=temp_dir),
            ), patch.object(global_dispatcher, "data_root", temp_dir), patch.object(
                global_dispatcher.reply_skill, "send_reply", new_callable=AsyncMock
            ) as mock_send_reply, patch(
                "litellm.acompletion", return_value=mock_resp
            ):
                tenant_directory.reload_tenants()
                mock_send_reply.return_value = {"success": True, "message_sid": "wamid.reply"}

                response = self._post(body, self._sign(body))
                self.assertEqual(response.status_code, 200)

                mock_send_reply.assert_called_once()
                self.assertEqual(mock_send_reply.call_args.kwargs.get("to_number"), "+971501234567")
                self.assertIn("Standard deposit is 50% upfront", mock_send_reply.call_args.kwargs.get("message"))

                db_mgr = TenantDatabaseManager(tenant_id="tenant_curtains_001", base_dir=temp_dir)
                with db_mgr._get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT status FROM inbound_messages WHERE message_sid = ?;", (message_id,))
                    row = cursor.fetchone()
                    self.assertIsNotNone(row)
                    self.assertEqual(row["status"], "completed")

                    cursor.execute(
                        "SELECT total_tokens, cost_usd FROM token_usage WHERE message_sid = ?;", (message_id,)
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