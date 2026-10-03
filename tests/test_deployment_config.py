import re
import unittest
from pathlib import Path

import yaml

from src.utils.alerts import EmailAlertChannel, send_platform_alert

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestDeploymentConfig(unittest.TestCase):
    def setUp(self):
        self.compose = yaml.safe_load((REPO_ROOT / "docker" / "docker-compose.yml").read_text())
        self.gateway = self.compose["services"]["gateway"]
        self.caddy = self.compose["services"]["caddy"]

    def test_app_is_not_published_only_caddy_is(self):
        self.assertNotIn("ports", self.gateway)
        self.assertEqual(self.gateway["expose"], ["8000"])
        self.assertEqual(sorted(self.caddy["ports"]), ["443:443", "443:443/udp", "80:80"])

    def test_uvicorn_trusts_proxy_headers_with_single_worker(self):
        command = " ".join(self.gateway["command"].split())
        self.assertIn("--proxy-headers", command)
        self.assertIn("--forwarded-allow-ips=*", command)
        self.assertIn("--workers 1", command)

    def test_secrets_are_required(self):
        env = dict(item.split("=", 1) for item in self.gateway["environment"])
        for name in [
            "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_NUMBER",
            "OPENAI_API_KEY", "ALERT_EMAIL", "SMTP_USER", "SMTP_PASSWORD",
        ]:
            self.assertRegex(env[name], rf"^\$\{{{name}:\?", f"{name} must be required")
        caddy_env = dict(item.split("=", 1) for item in self.caddy["environment"])
        self.assertRegex(caddy_env["DOMAIN"], r"^\$\{DOMAIN:\?")

    def test_caddy_keeps_certificates_and_blocks_metrics(self):
        self.assertIn("caddy_data:/data", self.caddy["volumes"])
        self.assertIn("caddy_data", self.compose["volumes"])
        caddyfile = (REPO_ROOT / "docker" / "caddy" / "Caddyfile").read_text()
        self.assertIn("{$DOMAIN}", caddyfile)
        blocked = re.search(r"@internal path (.+)", caddyfile).group(1).split()
        self.assertIn("/metrics", blocked)
        self.assertIn("/metrics/*", blocked)
        self.assertIn("respond @internal", caddyfile)
        self.assertIn("reverse_proxy gateway:8000", caddyfile)

    def test_unused_packages_removed(self):
        packages = [
            re.split(r"[<>=\[]", line.strip())[0].lower()
            for line in (REPO_ROOT / "requirements.txt").read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertNotIn("openhuman", packages)
        self.assertNotIn("asyncio", packages)
        self.assertEqual(len(packages), len(set(packages)), "duplicate package lines")

    def test_alert_email_comes_from_env_not_code(self):
        example = (REPO_ROOT / ".env.example").read_text()
        self.assertRegex(example, r"(?m)^ALERT_EMAIL=you@example\.com$")
        for path in (REPO_ROOT / "src").rglob("*.py"):
            self.assertNotRegex(path.read_text(), r"[\w.]+@gmail\.com", str(path))


class TestEmailAlerts(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_email_is_skipped_not_crashing(self):
        channel = EmailAlertChannel(to_address="", username="", password="")
        self.assertFalse(channel.is_configured())
        results = await send_platform_alert("subject", "body", email_channel=channel)
        self.assertEqual(results, {})


if __name__ == "__main__":
    unittest.main()
