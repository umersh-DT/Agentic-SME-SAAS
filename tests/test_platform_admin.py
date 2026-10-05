import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import hashlib
import hmac
from fastapi.testclient import TestClient
import yaml

from src.gateway.main import app
from src.gateway.platform_admin import handle_admin_command
from src.gateway.whatsapp_webhook import StrictTenantDirectory, tenant_directory

ADMIN = "+251701280019"
START = {"tenants": [{
    "tenant_id": "tenant_umer_test", "business_name": "Umer Test Business", "plan_tier": "starter",
    "subscription_status": "active", "owner_phone": ADMIN, "staff_phones": [],
    "enabled_skills": ["invoicing", "memory_tree"],
}]}


class TestAdminCommands(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "tenants.local.yaml")
        with open(self.path, "w") as f:
            yaml.safe_dump(START, f)
        self.env = patch.dict(os.environ, {"TENANTS_CONFIG_PATH": self.path})
        self.env.start()
        self.directory = StrictTenantDirectory(config_path=self.path)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def saved(self):
        with open(self.path) as f:
            return {t["tenant_id"]: t for t in yaml.safe_load(f)["tenants"]}

    async def run_cmd(self, text):
        return await handle_admin_command(text, self.directory)

    async def test_add_business_routes_owner_immediately(self):
        reply = await self.run_cmd("add business Ali Cleaning owner +971 52 993 7837")
        self.assertEqual(reply, "Added Ali Cleaning with owner +971529937837. They can message the assistant now.")
        self.assertEqual(self.saved()["tenant_ali_cleaning"]["owner_phone"], "+971529937837")
        self.assertEqual(self.directory.resolve_sender("+971529937837"), "tenant_ali_cleaning")

    async def test_staff_and_settings_changes(self):
        await self.run_cmd("add business Ali Cleaning owner +971529937837")
        self.assertEqual(await self.run_cmd("add staff 971500000002 to ali cleaning"),
                         "Added +971500000002 as staff at Ali Cleaning.")
        self.assertEqual(self.directory.resolve_sender("+971500000002"), "tenant_ali_cleaning")
        self.assertEqual(await self.run_cmd("set website aliclean.example.com for Ali Cleaning"),
                         "Set website for Ali Cleaning to https://aliclean.example.com.")
        self.assertEqual(await self.run_cmd("set calendar ali.owner@gmail.com for Ali Cleaning"),
                         "Set calendar for Ali Cleaning to ali.owner@gmail.com.")
        self.assertEqual(await self.run_cmd("set hours 10:00-19:00 for Ali Cleaning"),
                         "Set hours for Ali Cleaning to 10:00-19:00.")
        entry = self.saved()["tenant_ali_cleaning"]
        self.assertEqual((entry["website_url"], entry["google_calendar_id"], entry["business_hours"]),
                         ("https://aliclean.example.com", "ali.owner@gmail.com", "10:00-19:00"))
        self.assertEqual(self.directory.tenants["tenant_ali_cleaning"].google_calendar_id, "ali.owner@gmail.com")

        self.assertEqual(await self.run_cmd("remove staff +971500000002 from Ali Cleaning"),
                         "Removed +971500000002 from staff at Ali Cleaning.")
        self.assertIsNone(self.directory.resolve_sender("+971500000002"))

    async def test_list_and_remove_business(self):
        await self.run_cmd("add business Ali Cleaning owner +971529937837")
        listed = await self.run_cmd("list businesses")
        self.assertTrue(listed.startswith("2 businesses:"))
        self.assertIn("• Ali Cleaning — owner +971529937837, 0 staff", listed)
        removed = await self.run_cmd("remove business Ali Cleaning")
        self.assertTrue(removed.startswith("Removed Ali Cleaning from the business list."))
        self.assertNotIn("tenant_ali_cleaning", self.saved())
        self.assertIsNone(self.directory.resolve_sender("+971529937837"))

    async def test_invalid_changes_are_refused_and_file_untouched(self):
        with open(self.path) as f:
            before = f.read()
        dup = await self.run_cmd(f"add business Copycat owner {ADMIN}")
        self.assertEqual(dup, "Not saved: +2517***19 already belongs to Umer Test Business. "
                              "A number can only belong to one business.")
        bad_tz = await self.run_cmd("set timezone Mars/Base for Umer Test Business")
        self.assertIn("Not saved:", bad_tz)
        self.assertIn("Unknown timezone", bad_tz)
        missing = await self.run_cmd("add staff +971500000009 to Nobody Ltd")
        self.assertEqual(missing, 'I couldn\'t find a business called "Nobody Ltd". Send "list businesses" to see them.')
        with open(self.path) as f:
            self.assertEqual(f.read(), before)


class TestAdminOverWebhook(unittest.TestCase):
    def _post(self, client, sender_digits, body, msg_id):
        payload = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "1"},
            "messages": [{"from": sender_digits, "id": msg_id, "type": "text", "text": {"body": body}}],
        }}]}]}).encode()
        sig = "sha256=" + hmac.new(b"secret", payload, hashlib.sha256).hexdigest()
        return client.post("/webhook/whatsapp", content=payload,
                           headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})

    def test_only_admin_numbers_can_run_admin_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tenants.local.yaml")
            with open(path, "w") as f:
                yaml.safe_dump(START, f)
            env = {"TENANTS_CONFIG_PATH": path, "TENANTS_DATA_DIR": tmp, "WHATSAPP_APP_SECRET": "secret",
                   "WHATSAPP_PHONE_NUMBER_ID": "1", "PLATFORM_ADMIN_PHONES": ADMIN}
            with patch.dict(os.environ, env), \
                 patch("src.gateway.whatsapp_webhook.reply_skill.send_reply", AsyncMock(return_value={"success": True})) as send, \
                 patch("src.gateway.whatsapp_webhook.dispatcher.process_incoming_message", AsyncMock()) as dispatch:
                tenant_directory.reload_tenants()
                client = TestClient(app)
                admin = self._post(client, "251701280019", "add business Safdan Curtains owner +971581996182", "wamid.a1")
                stranger = self._post(client, "971509990000", "add business Evil Corp owner +971509990000", "wamid.a2")
                safdan = self._post(client, "971581996182", "Hello", "wamid.a3")
            tenant_directory.reload_tenants()

        self.assertEqual(admin.json()["messages"], ["admin_command"])
        self.assertEqual(send.call_args_list[0].kwargs["message"],
                         "Added Safdan Curtains with owner +971581996182. They can message the assistant now.")
        self.assertEqual(stranger.json()["messages"], ["unregistered_notified"])
        self.assertEqual(safdan.json()["messages"], ["dispatched"])
        self.assertEqual(dispatch.call_args.kwargs["tenant_id"], "tenant_safdan_curtains")


if __name__ == "__main__":
    unittest.main()
