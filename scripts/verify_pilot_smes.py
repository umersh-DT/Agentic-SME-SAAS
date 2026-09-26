import os
import sys
import yaml

CONFIG_PATH = os.environ.get("TENANTS_CONFIG_PATH", "config/tenants.yaml")

def main():
    if not os.path.exists(CONFIG_PATH):
        print(f"Error: {CONFIG_PATH} not found.")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    tenants = data.get("tenants", [])
    print(f"Verified {len(tenants)} active tenants in {CONFIG_PATH}:")
    for t in tenants:
        tid = t.get("tenant_id") or t.get("id")
        phone = t.get("whatsapp_number") or t.get("owner_phone")
        print(f" - [{tid}] {t.get('business_name')} ({phone}) | Tier: {t.get('plan_tier', 'starter')}")

if __name__ == "__main__":
    main()