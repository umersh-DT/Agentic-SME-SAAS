"""Simulated WhatsApp conversation against the real app.

Runs the real FastAPI app with Meta WhatsApp Cloud API webhooks (verification, X-Hub-Signature-256
checks, routing, dispatcher, business memory, invoicing tools and SQLite storage).
Only external services are stubbed:
  * the AI model (litellm.acompletion) - a tiny scripted stand-in, no API key needed
  * Gemini voice transcription HTTP - returns a fixed transcript
  * Meta Graph API HTTP (send text, upload PDF, send document, download voice note) - captured and printed

Usage:  python scripts/simulate_conversation.py
"""
import hashlib
import hmac
import json
import json as json_module
import os
import re
import shutil
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.chdir(REPO_ROOT)

WORK_DIR = tempfile.mkdtemp(prefix="wa_sim_")
APP_SECRET = "simulated_meta_app_secret"
VERIFY_TOKEN = "simulated_verify_token"
PHONE_NUMBER_ID = "109876543210"
OWNER = "+971501234567"   # Luxe Curtain Interiors owner (config/tenants.yaml)
STAFF = "+971502223344"   # staff member added for this simulation
STRANGER = "+447700900123"

# Settings must exist before the app modules are imported.
os.environ.update(
    {
        "WHATSAPP_APP_SECRET": APP_SECRET,
        "WHATSAPP_VERIFY_TOKEN": VERIFY_TOKEN,
        "WHATSAPP_TOKEN": "simulated_access_token",
        "WHATSAPP_PHONE_NUMBER_ID": PHONE_NUMBER_ID,
        "GEMINI_API_KEY": "simulated_gemini_key",
        "TENANTS_DATA_DIR": os.path.join(WORK_DIR, "tenants"),
        "TENANTS_CONFIG_PATH": os.path.join(WORK_DIR, "tenants.yaml"),
        "ALERT_EMAIL": "",
        "LLM_MODEL": "gemini/gemini-2.5-flash",
        "PLATFORM_ALERT_WHATSAPP": "",
    }
)
os.makedirs(os.environ["TENANTS_DATA_DIR"])
shutil.copy("config/tenants.yaml", os.environ["TENANTS_CONFIG_PATH"])
with open(os.environ["TENANTS_CONFIG_PATH"], encoding="utf-8") as f:
    cfg_text = f.read()
with open(os.environ["TENANTS_CONFIG_PATH"], "w", encoding="utf-8") as f:
    f.write(cfg_text.replace(
        'owner_phone: "+971501234567"\n    staff_phones: []',
        f'owner_phone: "+971501234567"\n    staff_phones: ["{STAFF}"]',
        1,
    ))

import logging  # noqa: E402

logging.disable(logging.CRITICAL)

from fastapi.testclient import TestClient  # noqa: E402
import httpx  # noqa: E402

from src.core.storage_models import TenantDatabaseManager  # noqa: E402
from src.gateway.main import app  # noqa: E402
from src.gateway.whatsapp_webhook import dispatcher, tenant_directory  # noqa: E402

outbox = []


# ---------------- Stub 1: the AI model ----------------
def _msg(content=None, tool_calls=None):
    data = {"role": "assistant", "content": content}
    if tool_calls:
        data["tool_calls"] = [
            {"id": t.id, "type": "function", "function": {"name": t.function.name, "arguments": t.function.arguments}}
            for t in tool_calls
        ]
    return SimpleNamespace(content=content, tool_calls=tool_calls, to_dict=lambda: data)


async def fake_ai(model, messages, **kwargs):
    """Scripted stand-in for the AI: answers from the facts it is given, calls the invoice tool when asked."""
    usage = SimpleNamespace(prompt_tokens=420, completion_tokens=60)
    last = messages[-1]
    if last["role"] == "tool":
        reply = _msg(content=f"Here is the draft:\n{last['content']}")
    else:
        text = last["content"]
        amount = re.search(r"([\d,]+(?:\.\d+)?)\s*AED", text, re.IGNORECASE)
        if text.lower().startswith("invoice") and amount:
            name = text.split()[1]
            call = SimpleNamespace(
                id="call_1",
                function=SimpleNamespace(
                    name="create_invoice",
                    arguments=json.dumps({"customer_name": name, "amount": float(amount.group(1).replace(",", "")),
                                          "description": "Blackout curtains"}),
                ),
            )
            reply = _msg(tool_calls=[call])
        elif not text.rstrip().endswith("?"):
            reply = _msg(content="Okay.")
        else:
            system = messages[0]["content"]
            facts = re.search(r"Do Not Execute As Instructions\):\n(.*?)\n### End", system, re.S).group(1)
            reply = _msg(content=f"Based on our records:\n{facts}" if "None currently" not in facts else "Got it.")
    return SimpleNamespace(choices=[SimpleNamespace(message=reply)], usage=usage)


# ---------------- Stub 2: external HTTP (Meta Graph API, Gemini transcription) ----------------
VOICE_TRANSCRIPT = "How much deposit do we take on curtain orders?"
uploads = {}


async def fake_http_post(self, url, json=None, headers=None, files=None, **kwargs):
    request = httpx.Request("POST", url)
    if "generativelanguage.googleapis.com" in url:
        return httpx.Response(200, request=request, json={
            "candidates": [{"content": {"parts": [{"text": VOICE_TRANSCRIPT}]}}],
            "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 15},
        })
    assert url.startswith(f"https://graph.facebook.com/") and (headers or {}).get("Authorization", "").startswith("Bearer ")
    if url.endswith("/media"):
        media_id = f"UPLOADED_{len(uploads) + 1}"
        uploads[media_id] = files["file"]
        return httpx.Response(200, request=request, json={"id": media_id})
    outbox.append(json)
    return httpx.Response(200, request=request, json={"messages": [{"id": f"wamid.out_{len(outbox)}"}]})


async def fake_http_get(self, url, headers=None, **kwargs):
    request = httpx.Request("GET", url)
    if url.endswith("/MEDIA_1"):
        return httpx.Response(200, request=request, json={"url": "https://lookaside.fbsbx.com/voice", "mime_type": "audio/ogg; codecs=opus"})
    return httpx.Response(200, request=request, content=b"OggS-simulated-voice-note")


def signed_post(client, sender, message):
    """Posts a Meta-format webhook (sender digits without '+') signed with the app secret."""
    body = json_module.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "15550001111", "phone_number_id": PHONE_NUMBER_ID},
            "messages": [dict({"from": sender.lstrip("+"), "timestamp": "1730000000"}, **message)],
        }}]}],
    }).encode()
    signature = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhook/whatsapp", content=body,
                       headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"})


def show(label, sender, shown_text, response):
    print(f"\n[{label} {sender}] > {shown_text}")
    if response.status_code != 200:
        print(f"  (rejected: HTTP {response.status_code})")
    if "duplicate" in response.json().get("messages", []):
        print("  (duplicate delivery dropped - no second reply)")
    while outbox:
        out = outbox.pop(0)
        if out["type"] == "document":
            doc = out["document"]
            name, pdf, _ = uploads[doc["id"]]
            marked = "DRAFT watermark" if b"DRAFT" in pdf else "no watermark"
            print(f"  < (to {out['to']}) [PDF {name}, {len(pdf):,} bytes, {marked}] {doc['caption']}")
        else:
            print(f"  < (to {out['to']}) " + out["text"]["body"].replace("\n", "\n    "))


def main():
    tenant_directory.reload_tenants()
    dispatcher.data_root = os.environ["TENANTS_DATA_DIR"]
    def text(body, msg_id):
        return {"id": msg_id, "type": "text", "text": {"body": body}}

    script = [
        ("OWNER", OWNER, "Remember: deposits are 50% upfront for all curtain orders.", text("Remember: deposits are 50% upfront for all curtain orders.", "wamid.sim_1")),
        ("STAFF", STAFF, "From now on we give every customer a 20% discount.", text("From now on we give every customer a 20% discount.", "wamid.sim_2")),
        ("STAFF", STAFF, "What deposit do we take on curtain orders?", text("What deposit do we take on curtain orders?", "wamid.sim_3")),
        ("OWNER", OWNER, "Invoice Ali 2,500 AED for blackout curtains", text("Invoice Ali 2,500 AED for blackout curtains", "wamid.sim_4")),
        ("OWNER", OWNER, f"(voice note: \"{VOICE_TRANSCRIPT}\")", {"id": "wamid.sim_5", "type": "audio", "audio": {"id": "MEDIA_1", "voice": True}}),
        ("STAFF", STAFF, "approve invoice 1001", text("approve invoice 1001", "wamid.sim_7")),
        ("OWNER", OWNER, "approve invoice 1001", text("approve invoice 1001", "wamid.sim_8")),
        ("OWNER", OWNER, "Remember: we never work on Fridays", text("Remember: we never work on Fridays", "wamid.sim_9")),
        ("OWNER", OWNER, "list rules", text("list rules", "wamid.sim_10")),
        ("OWNER", OWNER, "change rule 1: Deposits are 30% upfront for all curtain orders.", text("change rule 1: Deposits are 30% upfront for all curtain orders.", "wamid.sim_11")),
        ("OWNER", OWNER, "forget rule 2", text("forget rule 2", "wamid.sim_12")),
        ("STAFF", STAFF, "list rules", text("list rules", "wamid.sim_13")),
        ("STAFF", STAFF, "help", text("help", "wamid.sim_14")),
        ("UNKNOWN", STRANGER, "hi, is this the curtain shop?", text("hi, is this the curtain shop?", "wamid.sim_6")),
        ("OWNER", OWNER, "Remember: deposits are 50% upfront for all curtain orders. (Meta re-delivers)", text("Remember: deposits are 50% upfront for all curtain orders.", "wamid.sim_1")),
    ]
    with patch("litellm.acompletion", fake_ai), patch.object(httpx.AsyncClient, "post", fake_http_post), \
            patch.object(httpx.AsyncClient, "get", fake_http_get):
        client = TestClient(app)
        check = client.get("/webhook/whatsapp", params={
            "hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "8675309"})
        print(f"[META] webhook verification -> HTTP {check.status_code}, body {check.text!r}")
        for label, sender, shown, message in script:
            show(label, sender, shown, signed_post(client, sender, message))

    db = TenantDatabaseManager("tenant_curtains_001", base_dir=os.environ["TENANTS_DATA_DIR"])
    with db._get_connection() as conn:
        statuses = conn.execute("SELECT message_sid, status FROM inbound_messages ORDER BY created_at").fetchall()
        usage = conn.execute("SELECT COUNT(*) n, SUM(total_tokens) t, SUM(cost_usd) c FROM token_usage").fetchone()
        invoice = conn.execute("SELECT invoice_number, subtotal, tax_amount, grand_total, status FROM invoices").fetchone()
        voice_cost = conn.execute("SELECT cost_usd FROM token_usage WHERE model LIKE '%(voice)'").fetchone()
    print("\n--- Stored for Luxe Curtain Interiors ---")
    print("messages:", [tuple(r) for r in statuses])
    print("invoice:", tuple(invoice))
    print(f"AI model: {os.environ['LLM_MODEL']}")
    print(f"Voice note transcription cost recorded: ${voice_cost['cost_usd']:.6f}")
    print(f"AI usage recorded: {usage['n']} turns, {usage['t']} tokens, ${usage['c']:.6f}")
    shutil.rmtree(WORK_DIR)


if __name__ == "__main__":
    main()
