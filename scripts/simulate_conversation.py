"""Simulated WhatsApp conversation against the real app.

Runs the real FastAPI app, webhook signature checks, routing, dispatcher, business memory,
invoicing tools and SQLite storage. Only two things are stubbed:
  * the AI model (litellm.acompletion) - a tiny scripted stand-in, no API key needed
  * the outbound WhatsApp HTTP call - captured and printed instead of sent

Usage:  python scripts/simulate_conversation.py
"""
import base64
import hashlib
import hmac
import json
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
AUTH_TOKEN = "simulated_twilio_auth_token"
OWNER = "+971501234567"   # Luxe Curtain Interiors owner (config/tenants.yaml)
STAFF = "+971502223344"   # staff member added for this simulation
STRANGER = "+447700900123"

# Settings must exist before the app modules are imported.
os.environ.update(
    {
        "TWILIO_AUTH_TOKEN": AUTH_TOKEN,
        "TWILIO_ACCOUNT_SID": "ACsimulated000000000000000000000000",
        "TWILIO_WHATSAPP_NUMBER": "+14155238886",
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
from src.gateway.twilio_webhook import dispatcher, tenant_directory  # noqa: E402

WEBHOOK_URL = "http://testserver/webhook/whatsapp"
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


# ---------------- Stub 2: outbound WhatsApp HTTP ----------------
async def fake_twilio_post(self, url, data=None, auth=None, **kwargs):
    outbox.append(data)
    return httpx.Response(201, json={"sid": f"SM_out_{len(outbox)}"}, request=httpx.Request("POST", url))


def signed_post(client, sender, body, sid):
    params = {"From": f"whatsapp:{sender}", "To": "whatsapp:+14155238886", "Body": body, "MessageSid": sid}
    concatenated = WEBHOOK_URL + "".join(f"{k}{v}" for k, v in sorted(params.items()))
    signature = base64.b64encode(hmac.new(AUTH_TOKEN.encode(), concatenated.encode(), hashlib.sha1).digest()).decode()
    return client.post("/webhook/whatsapp", data=params, headers={"X-Twilio-Signature": signature})


def show(label, sender, body, response):
    print(f"\n[{label} {sender}] > {body}")
    twiml = re.search(r"<Message>(.*?)</Message>", response.text, re.S)
    if twiml:
        print(f"  < (assistant) {twiml.group(1).strip()}")
    if response.headers.get("X-Dedup-Dropped"):
        print("  (duplicate delivery dropped - no second reply)")
    while outbox:
        out = outbox.pop(0)
        print(f"  < (to {out['To']}) " + out["Body"].replace("\n", "\n    "))


def main():
    tenant_directory.reload_tenants()
    dispatcher.data_root = os.environ["TENANTS_DATA_DIR"]
    script = [
        ("OWNER", OWNER, "Remember: deposits are 50% upfront for all curtain orders.", "SM_sim_1"),
        ("STAFF", STAFF, "From now on we give every customer a 20% discount.", "SM_sim_2"),
        ("STAFF", STAFF, "What deposit do we take on curtain orders?", "SM_sim_3"),
        ("OWNER", OWNER, "Invoice Ali 2,500 AED for blackout curtains", "SM_sim_4"),
        ("UNKNOWN", STRANGER, "hi, is this the curtain shop?", "SM_sim_5"),
        ("OWNER", OWNER, "Remember: deposits are 50% upfront for all curtain orders.", "SM_sim_1"),
    ]
    with patch("litellm.acompletion", fake_ai), patch.object(httpx.AsyncClient, "post", fake_twilio_post):
        client = TestClient(app)
        for label, sender, body, sid in script:
            show(label, sender, body, signed_post(client, sender, body, sid))

    db = TenantDatabaseManager("tenant_curtains_001", base_dir=os.environ["TENANTS_DATA_DIR"])
    with db._get_connection() as conn:
        statuses = conn.execute("SELECT message_sid, status FROM inbound_messages ORDER BY created_at").fetchall()
        usage = conn.execute("SELECT COUNT(*) n, SUM(total_tokens) t, SUM(cost_usd) c FROM token_usage").fetchone()
        invoice = conn.execute("SELECT invoice_number, subtotal, tax_amount, grand_total, status FROM invoices").fetchone()
    print("\n--- Stored for Luxe Curtain Interiors ---")
    print("messages:", [tuple(r) for r in statuses])
    print("invoice:", tuple(invoice))
    print(f"AI model: {os.environ['LLM_MODEL']}")
    print(f"AI usage recorded: {usage['n']} turns, {usage['t']} tokens, ${usage['c']:.6f}")
    shutil.rmtree(WORK_DIR)


if __name__ == "__main__":
    main()
